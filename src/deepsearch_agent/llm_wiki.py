"""Evidence-backed Markdown wiki, separate from conversation working memory."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from deepsearch_agent.local_files import atomic_write
from deepsearch_agent.bm25 import BM25Index


PAGE_ID = r'wiki_[a-f0-9]{32}'


class WikiError(ValueError):
    """Public, data-free failure code."""


class WikiSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chunk_id: str = Field(pattern=r'^chk_[a-f0-9]{64}$')
    source_path: str = Field(min_length=1, max_length=2000)
    page_number: int = Field(ge=1)
    content_id: str = ''


class WikiPage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern='^' + PAGE_ID + '$')
    title: str = Field(min_length=1, max_length=160)
    markdown: str = Field(min_length=1, max_length=20000)
    sources: list[WikiSource] = Field(min_length=1, max_length=100)
    related: list[str] = Field(default_factory=list, max_length=20)
    updated_at: str
    revision: str = ''
    question: str = Field(default='', max_length=2000)
    compiler_model: str = 'unknown'
    prompt_version: str = 'wiki-v1'


class WikiStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = RLock()

    def _path(self, page_id):
        if not isinstance(page_id, str) or not re.fullmatch(PAGE_ID, page_id):
            raise WikiError('wiki_invalid_id')
        path = self.root / (page_id + '.md')
        if path.resolve() != path or path.is_symlink() or path.is_junction():
            raise WikiError('wiki_unsafe_path')
        return path

    def get(self, page_id):
        with self.lock:
            path = self._path(page_id)
            if not path.exists():
                raise WikiError('wiki_not_found')
            if path.stat().st_size > 200000:
                raise WikiError('wiki_invalid_page')
            try:
                raw = path.read_bytes()
                header, body = raw.decode('utf-8').split('\n---\n', 1)
                if not header.startswith('---\n'):
                    raise ValueError
                page = WikiPage.model_validate({**json.loads(header[4:]), 'markdown': body,
                                               'revision': hashlib.sha256(raw).hexdigest()})
                if page.id != page_id:
                    raise ValueError
                return page
            except (ValueError, TypeError):
                raise WikiError('wiki_invalid_page') from None

    def list_pages(self):
        with self.lock:
            pages = []
            for path in sorted(self.root.glob('wiki_*.md')):
                pages.append(self.get(path.stem))
            return pages

    def save(self, page, expected_revision=None):
        with self.lock:
            page = WikiPage.model_validate(page.model_dump())
            path = self._path(page.id)
            current = self.get(page.id).revision if path.exists() else None
            if current != expected_revision:
                raise WikiError('wiki_conflict')
            for related in page.related:
                if related == page.id or not self._path(related).exists():
                    raise WikiError('wiki_invalid_link')
            metadata = page.model_dump(exclude={'markdown', 'revision'})
            raw = '---\n' + json.dumps(metadata, ensure_ascii=False) + '\n---\n' + page.markdown
            if len(raw.encode('utf-8')) > 200000:
                raise WikiError('wiki_too_large')
            atomic_write(path, raw.encode('utf-8'))
            return self.get(page.id)

    def delete(self, page_id, expected_revision):
        with self.lock:
            if self.get(page_id).revision != expected_revision:
                raise WikiError('wiki_conflict')
            self._path(page_id).unlink()

    def lint(self, registry):
        pages = self.list_pages()
        ids = {page.id for page in pages}
        issues = []
        for page in pages:
            for source in page.sources:
                current = registry.get(source.chunk_id)
                if current is None or current.source_path != source.source_path:
                    issues.append({'page_id': page.id, 'code': 'stale_source', 'chunk_id': source.chunk_id})
            for related in page.related:
                if related not in ids:
                    issues.append({'page_id': page.id, 'code': 'broken_link', 'target': related})
            if not page.related and not any(page.id in other.related for other in pages):
                issues.append({'page_id': page.id, 'code': 'orphan_page'})
        return issues


class WikiView:
    def __init__(self, store, sources, registry):
        self.store, self.sources, self.registry = store, frozenset(sources), registry

    def allows(self, page):
        return all(source.source_path in self.sources and source.chunk_id in self.registry
                   and self.registry[source.chunk_id].source_path == source.source_path
                   for source in page.sources)

    def pages(self):
        return [page for page in self.store.list_pages() if self.allows(page)]

    def search(self, query, limit=5):
        pages = self.pages()
        index = BM25Index([page.title + '\n' + page.markdown for page in pages])
        return [{'id': page.id, 'title': page.title, 'excerpt': page.markdown[:400],
                 'revision': page.revision} for page in (pages[i] for i in index.rank(query, limit))]

    def read(self, page_id):
        page = self.store.get(page_id)
        if not self.allows(page):
            raise WikiError('wiki_not_available')
        visible = {item.id for item in self.pages()}
        result = page.model_dump()
        result['related'] = [related for related in page.related if related in visible]
        result['backlinks'] = [p.id for p in self.pages() if page_id in p.related]
        return {**result, 'trust': 'derived_navigation_not_evidence'}


class WikiCompiler:
    def __init__(self, service):
        self.service = service

    def compile(self, title, question, sources, *, page_id=None, top_k=5):
        from deepsearch_agent.research_graph import ResearchWorkflow
        service = self.service
        kb = service.knowledge_base.restrict_sources(set(sources))
        outcomes = []
        def observe(kind, payload):
            if kind == 'rag.outcome':
                outcomes.append(payload)
        # Compile only indexed evidence: transient tool-created OCR chunks cannot
        # be resolved by stable ID in a later request until they are indexed.
        workflow = ResearchWorkflow(service.client, kb, context_window=service.context_window,
            max_output_tokens=service.max_output_tokens, reranker=service.reranker,
            visual_evidence=service.visual_evidence, observer=observe)
        answer = workflow.answer(question, top_k, [])
        if not outcomes or outcomes[-1].get('status') != 'verified' or not answer.sources:
            raise WikiError('wiki_unverified')
        if any(source.source_path not in sources for source in answer.sources):
            raise WikiError('wiki_source_not_allowed')
        refs = [WikiSource(**{key: asdict(source)[key] for key in WikiSource.model_fields})
                for source in answer.sources]
        return WikiPage(id=page_id or 'wiki_' + uuid4().hex, title=title.strip(),
            markdown=answer.text, sources=refs, updated_at=datetime.now(timezone.utc).isoformat(),
            question=question, compiler_model=getattr(getattr(service.client, 'settings', None), 'model', 'unknown'))
