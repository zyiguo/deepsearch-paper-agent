"""Scoped paper tools shared by the agent and protocol adapters."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.tool_execution import ToolKind, ToolExecutionBlocked, ToolNotExecuted
from deepsearch_agent.core.cancellation import ResearchCancelled
from deepsearch_agent.core.identity import source_identity
from deepsearch_tracing.context import notify
from deepsearch_agent.knowledge.retrieval_pipeline import retrieve


class ToolError(ValueError):
    """A deliberately public, data-free error code."""


class ToolRejected(ToolError, ToolNotExecuted):
    """A local check proves no paid request or requested write was made."""
    def __init__(self, code):
        ToolNotExecuted.__init__(self, code)


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class NoArgs(ToolArgs):
    pass


class SearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=2000)
    top_k: StrictInt = Field(default=5, ge=1, le=10)
    sources: list[str] | None = Field(default=None, max_length=100)


class PageArgs(ToolArgs):
    source: str = Field(min_length=1, max_length=2000)
    page: StrictInt = Field(ge=1, le=100000)


class EvidenceArgs(ToolArgs):
    chunk_id: str = Field(pattern=r'^chk_[a-f0-9]{64}$')


class WorkingMemoryArgs(ToolArgs):
    action: Literal['read', 'replace']
    document: Literal['plan', 'history']
    content: str | None = Field(default=None, max_length=2000)
    expected_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def check_action(self):
        if self.action == 'replace' and (self.content is None or self.expected_revision is None):
            raise ValueError('Replacement requires content and expected_revision')
        if self.action == 'read' and (self.content is not None or self.expected_revision is not None):
            raise ValueError('Read does not accept write arguments')
        return self


class WikiSearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=2000)
    limit: StrictInt = Field(default=5, ge=1, le=10)


class WikiReadArgs(ToolArgs):
    page_id: str = Field(pattern=r'^wiki_[a-f0-9]{32}$')


TOOLS = {
    'list_documents': (NoArgs, 'List documents in the authorized corpus.'),
    'search_evidence': (SearchArgs, 'Search text and image evidence; optionally narrow sources.'),
    'read_page': (PageArgs, 'Read an authorized original page and its evidence IDs.'),
    'ocr_page': (PageArgs, 'Recognize an authorized page image on demand; may incur model cost.'),
    'get_evidence': (EvidenceArgs, 'Read evidence by its stable chunk_id within the authorized corpus.'),
}


@dataclass
class ToolResult:
    content: str
    chunks: list[TextChunk] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def evidence_record(chunk):
    return {'chunk_id': chunk.chunk_id, 'content_id': chunk.content_id, 'asset_ids': chunk.asset_ids,
            'source': chunk.source_path, 'page': chunk.page_number, 'page_end': chunk.page_end,
            'evidence_kind': chunk.evidence_kind, 'visual_pages': chunk.visual_pages,
            'text': chunk.text[:4000]}


class PaperToolSession:
    def __init__(self, kb, *, settings=None, catalog=None, page_reader=None, image_resolver=None,
                 analyzer=None, cache=None, reranker=None, observer=None, input_evidence=None,
                 working_memory=None, wiki_store=None):
        self.kb = kb
        self.settings = settings or AgentSettings()
        self.page_reader, self.image_resolver = page_reader, image_resolver
        self.analyzer, self.cache, self.reranker = analyzer, cache, reranker
        self.observer = observer
        self.memory = working_memory
        self.memory_notes = {}
        self.execution = None
        self.operation_slot = None
        self.tool_definitions = dict(TOOLS)
        if working_memory is not None:
            self.tool_definitions['working_memory'] = (WorkingMemoryArgs,
                'Read or replace compact conversation working notes, not document evidence. '
                'plan is PLAN.md (pending steps); history is HISTORY.md (completed work and explicit '
                'user preferences). Read first; replace requires that revision and the complete Markdown '
                'document (max 2000 characters). Preserve useful existing notes. No filesystem paths.')
        images = getattr(kb, 'image_index', None)
        chunks = [*kb.chunks, *(images.chunks if images else []), *(input_evidence or [])]
        self.sources = frozenset(c.source_path for c in chunks)
        self.registry = {c.chunk_id: c for c in chunks}
        self.wiki = None
        if wiki_store is not None:
            from deepsearch_agent.workspace.llm_wiki import WikiView
            self.wiki = WikiView(wiki_store, self.sources, self.registry)
            self.tool_definitions.update({
                'search_wiki': (WikiSearchArgs, 'Search derived wiki notes within current document scope. '
                                'Navigation only; retrieve original evidence before citing any claim.'),
                'read_wiki': (WikiReadArgs, 'Read a scoped wiki page and original chunk IDs. '
                              'Use get_evidence on original chunk IDs; wiki text is not evidence.'),
            })
        self.catalog = {d['source_path']: {k: d[k] for k in ('source_path', 'title', 'page_count') if k in d}
                        for d in (catalog or []) if d.get('source_path') in self.sources}
        for chunk in chunks:
            record = self.catalog.setdefault(chunk.source_path, {'source_path': chunk.source_path,
                'title': chunk.source_path.rsplit('/', 1)[-1], 'page_count': 0})
            record['page_count'] = max(record.get('page_count', 0), chunk.page_end or chunk.page_number)
        self.evidence = []
        self.calls = 0
        self.ocr_calls = 0
        self.deadline = time.monotonic() + self.settings.timeout_seconds
        self.image_refs = {(c.source_path, p) for c in (input_evidence or []) for p in c.visual_pages}

    def runtime_snapshot(self):
        return {'calls': self.calls, 'ocr_calls': self.ocr_calls,
                'memory_reads': dict(self.memory.read_revisions) if self.memory is not None else {},
                'memory_scope_revision': self.memory.generation if self.memory is not None else None,
                'memory_notes': dict(self.memory_notes), 'evidence': list(self.evidence),
                'image_refs': sorted(self.image_refs)}

    def restore_runtime(self, snapshot):
        self.calls, self.ocr_calls = snapshot['calls'], snapshot['ocr_calls']
        self.memory_notes = dict(snapshot['memory_notes'])
        if self.memory is not None:
            for document in tuple(self.memory.read_revisions):
                self.memory.consume_read(document)
            for document, revision in snapshot.get('memory_reads', {}).items():
                self.memory.restore_read(document, revision, snapshot.get('memory_scope_revision'))
        self.evidence = []
        self.image_refs = {tuple(ref) for ref in snapshot['image_refs']}
        self.result({}, snapshot['evidence'])

    @property
    def remaining_calls(self):
        if time.monotonic() >= self.deadline:
            return 0
        return max(0, self.settings.max_tool_calls - self.calls)

    def reserve(self, name):
        if not self.remaining_calls:
            raise ToolRejected('tool_budget')
        self.calls += 1
        notify(self.observer, 'tool.request', {'name': name, 'call': self.calls})

    def specs(self):
        return [{'name': name, 'description': description, 'input_schema': schema.model_json_schema()}
                for name, (schema, description) in self.tool_definitions.items()]

    def invoke(self, name, arguments):
        if name not in self.tool_definitions:
            raise ToolError('unknown_tool')
        try:
            args = self.tool_definitions[name][0].model_validate(arguments)
        except ValidationError:
            raise ToolError('invalid_arguments') from None
        values = args.model_dump()
        if self.execution is not None:
            if self.operation_slot is None:
                raise ToolExecutionBlocked('tool_operation_identity_missing')
            try:
                outcome = self.execution.execute(self.operation_slot, name, self.tool_kind(name, values), values,
                    lambda ident: asdict(self._invoke_validated(name, values)))
            except ToolNotExecuted as error:
                if error.replayed:
                    self.reserve(name)
                raise ToolError(error.code) from None
            if outcome.replayed:
                self.reserve(name)
                if name == 'ocr_page':
                    self.ocr_calls += 1
                saved = outcome.value
                restored = self.result(saved['data'], [TextChunk(**c) for c in saved['chunks']])
                if name == 'working_memory':
                    if values['action'] == 'read':
                        self.memory.restore_read(values['document'], saved['data']['revision'], saved['data'].get('scope_revision'))
                    else:
                        self.memory.consume_read(values['document'])
                    self.memory_notes[values['document']] = saved['data'].get('content', values.get('content', ''))
                result = ToolResult(saved['content'], restored.chunks, saved['data'])
            else:
                saved = outcome.value
                result = ToolResult(saved['content'], [TextChunk(**c) for c in saved['chunks']], saved['data'])
            notify(self.observer, 'tool.execution', {'operation_id': outcome.operation_id,
                   'name': name, 'replayed': outcome.replayed, 'status': 'succeeded'})
            return result
        return self._invoke_validated(name, values)

    def tool_kind(self, name, arguments):
        if name == 'working_memory' and arguments.get('action') == 'replace':
            return ToolKind.EFFECTFUL
        if name == 'ocr_page' or (name == 'search_evidence' and (
                self.reranker is not None or getattr(self.kb, 'embedding_client', None) is not None
                or getattr(self.kb, 'image_index', None) is not None)):
            return ToolKind.PAID
        return ToolKind.READ_ONLY

    def _invoke_validated(self, name, arguments):
        self.reserve(name)
        try:
            result = getattr(self, name)(**arguments)
        except ResearchCancelled:
            raise
        except ToolError:
            raise
        except ToolNotExecuted:
            raise
        except Exception:
            notify(self.observer, 'tool.error', {'name': name, 'code': 'tool_failed'})
            raise ToolError('tool_failed') from None
        notify(self.observer, 'tool.result', {'name': name, 'chunk_ids': [c.chunk_id for c in result.chunks]})
        return result

    def working_memory(self, action, document, content=None, expected_revision=None):
        from deepsearch_agent.workspace.working_memory import WorkingMemoryError
        try:
            value = (self.memory.read(document) if action == 'read' else
                     self.memory.replace(document, content, expected_revision))
        except WorkingMemoryError as error:
            raise ToolRejected(str(error)) from None
        self.memory_notes[document] = value['content'] if action == 'read' else content
        return self.result(value)

    def search_wiki(self, query, limit=5):
        from deepsearch_agent.workspace.llm_wiki import WikiError
        try:
            return self.result({'pages': self.wiki.search(query, limit),
                                'trust': 'derived_navigation_not_evidence'})
        except WikiError as error:
            raise ToolError(str(error)) from None

    def read_wiki(self, page_id):
        from deepsearch_agent.workspace.llm_wiki import WikiError
        try:
            return self.result(self.wiki.read(page_id))
        except WikiError as error:
            raise ToolError(str(error)) from None

    def result(self, data, chunks=None):
        admitted = []
        keys = {c.chunk_id for c in self.evidence}
        for c in chunks or []:
            if c.source_path not in self.sources:
                continue
            refs = {(c.source_path, p) for p in c.visual_pages}
            if len(self.image_refs | refs) > self.settings.max_images:
                continue
            if c.chunk_id not in keys:
                if len(self.evidence) >= self.settings.max_evidence:
                    continue
                self.evidence.append(c)
                keys.add(c.chunk_id)
                self.image_refs.update(refs)
            self.registry[c.chunk_id] = c
            admitted.append(c)
        if chunks is not None:
            data = {**data, 'evidence': [evidence_record(c) for c in admitted]}
        content = json.dumps(data, ensure_ascii=False)
        if len(content) > self.settings.observation_chars:
            # Keep valid JSON and an explicit truncation marker.
            content = json.dumps({'truncated': True, 'excerpt': content[:self.settings.observation_chars // 2]},
                                 ensure_ascii=False)
        return ToolResult(content, admitted, data)

    def list_documents(self):
        values = sorted(self.catalog.values(), key=lambda d: d['source_path'])
        return self.result({'documents': values[:100], 'truncated': len(values) > 100})

    def search_evidence(self, query, top_k=5, sources=None):
        if not query.strip():
            raise ToolError('invalid_arguments')
        if sources is not None and not set(sources) <= self.sources:
            raise ToolRejected('source_not_allowed')
        kb = self.kb.restrict_sources(set(sources)) if sources is not None else self.kb
        wiki = self.wiki
        if sources is not None and wiki is not None:
            from deepsearch_agent.workspace.llm_wiki import WikiView
            allowed = {c.chunk_id: c for c in kb.chunks}
            images = getattr(kb, 'image_index', None)
            if images is not None:
                allowed.update((c.chunk_id, c) for c in images.chunks)
            wiki = WikiView(wiki.store, set(sources), allowed)
        chunks = retrieve(kb, query.strip(), top_k, reranker=self.reranker, wiki_view=wiki)
        return self.result({'query': query.strip()}, chunks)

    def check_page(self, source, page):
        if source not in self.sources:
            raise ToolRejected('source_not_allowed')
        if page > self.catalog[source]['page_count']:
            raise ToolRejected('page_not_found')

    def read_page(self, source, page):
        self.check_page(source, page)
        if self.page_reader is None:
            raise ToolError('page_reader_unavailable')
        value = self.page_reader(source, page)
        text = value['text']
        if not isinstance(text, str) or len(text) > 100000:
            raise ToolError('invalid_page')
        visual = any(c.source_path == source and page in c.visual_pages for c in self.registry.values())
        chunks = [TextChunk(text, source, page, 1000000, visual_pages=[page] if visual else [])] if text.strip() else []
        # Empty scan pages remain addressable for the explicit OCR tool.
        return self.result({'source': source, 'page': page, 'image_available': self.image_resolver is not None}, chunks)

    def ocr_page(self, source, page):
        self.check_page(source, page)
        if self.analyzer is None or self.image_resolver is None:
            raise ToolRejected('ocr_unavailable')
        if self.ocr_calls >= self.settings.max_ocr_calls:
            raise ToolRejected('ocr_budget')
        if len(self.image_refs | {(source, page)}) > self.settings.max_images:
            raise ToolRejected('image_budget')
        self.ocr_calls += 1
        document_id = hashlib.sha256(source_identity(source).encode()).hexdigest()
        strategy = hashlib.sha256((self.analyzer.identity + ':tool-full-page-ocr-v1').encode()).hexdigest()[:24]
        analysis = self.cache.load(document_id, strategy, page) if self.cache else None
        cached = analysis is not None
        if analysis is None:
            analysis = self.analyzer.analyze(self.image_resolver(source, page), '')
            if self.cache:
                self.cache.save(document_id, strategy, page, analysis)
        chunks = []
        if analysis.ocr_text.strip():
            chunks.append(TextChunk(analysis.ocr_text, source, page, 2000000, evidence_kind='ocr', visual_pages=[page]))
        for i, item in enumerate(analysis.items, 2000001):
            chunks.append(TextChunk(f'{item.label}\n{item.description}'.strip(), source, page, i,
                                    evidence_kind=item.kind, visual_pages=[page]))
        return self.result({'source': source, 'page': page, 'cached': cached,
                            'warnings': analysis.warnings}, chunks)

    def get_evidence(self, chunk_id):
        if chunk_id not in self.registry:
            raise ToolError('evidence_not_found')
        return self.result({}, [self.registry[chunk_id]])
