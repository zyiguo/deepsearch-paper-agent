"""Question-answering service built on an LLMClient."""

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.llm import LLMClient
from deepsearch_agent.prompts import SYSTEM_PROMPT, build_qa_prompt
from deepsearch_agent.query_expander import QueryExpander
from deepsearch_agent.reranker import Reranker
from deepsearch_agent.tokens import estimate_tokens, truncate_text
from dataclasses import dataclass, field, replace as dc_replace, asdict
from deepsearch_agent.observability import notify
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepsearch_agent.storage import JsonlAnswerStore
    from deepsearch_agent.visual_evidence import VisualEvidenceProvider
@dataclass
class SourceReference:
    source_path: str
    page_number: int
    text: str
    page_end: int | None = None
    section_path: list[str] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    evidence_kind: str = 'text'
    visual_pages: list[int] = field(default_factory=list)
    chunk_id: str = ''
    content_id: str = ''
    asset_ids: list[str] = field(default_factory=list)


@dataclass
class Answer:
    text: str
    sources: list[SourceReference]
    skills_used: list[dict[str, str]] = field(default_factory=list)

def format_chunk_for_context(chunk: TextChunk) -> str:
    page = str(chunk.page_number) if not chunk.page_end or chunk.page_end == chunk.page_number else f'{chunk.page_number}–{chunk.page_end}'
    section = ('章节：' + ' > '.join(chunk.section_path) + '\n') if chunk.section_path else ''
    source = chunk_to_source_reference(chunk)
    body = '\n'.join(f"第 {p['page_number']} 页：\n{p['text']}" for p in source.pages) if source.pages else chunk.text
    return f"[来源: {chunk.source_path}, 第 {page} 页]\n{section}{body}"

def chunk_to_source_reference(chunk: TextChunk) -> SourceReference:
    return SourceReference(
        source_path=chunk.source_path,
        page_number=chunk.page_number,
        text=chunk.text,
        page_end=chunk.page_end,
        section_path=list(chunk.section_path),
        pages=[{'page_number': span['page_number'], 'text': chunk.text[span['chunk_start']:span['chunk_end']]}
               for span in chunk.page_spans],
        evidence_kind=chunk.evidence_kind,
        visual_pages=list(chunk.visual_pages),
        chunk_id=chunk.chunk_id,
        content_id=chunk.content_id,
        asset_ids=list(chunk.asset_ids),
    )
class AnswerService:
    def __init__(
        self,
        client: LLMClient,
        knowledge_base: KnowledgeBase | None = None,
        history_store: "JsonlAnswerStore | None" = None,
        use_graph: bool = False,
        context_window: int = 8192,
        max_output_tokens: int = 1024,
        reranker: Reranker | None = None,
        query_expander: QueryExpander | None = None,
        search_intent=None,
        run_recorder=None,
        visual_evidence: 'VisualEvidenceProvider | None' = None,
        skill_registry=None,
        agent_settings=None,
        tool_session_factory=None,
        mcp_settings=None,
    ):
        self.client = client
        self.knowledge_base = knowledge_base
        self.history_store = history_store
        self.use_graph = use_graph
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.reranker = reranker
        self.query_expander = query_expander
        self.search_intent = search_intent
        self.run_recorder = run_recorder
        self.visual_evidence = visual_evidence
        self.skill_registry = skill_registry
        self.agent_settings = agent_settings
        self.tool_session_factory = tool_session_factory
        self.mcp_settings = mcp_settings

    def answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources: set[str] | None = None,
    memory_context=None, trace_context=None, input_evidence: list[TextChunk] | None = None, skill_ids=None,
    working_memory=None) -> Answer:
        kwargs = dict(on_progress=on_progress, allowed_sources=allowed_sources, memory_context=memory_context,
                      input_evidence=input_evidence, skill_ids=skill_ids, working_memory=working_memory)
        if self.run_recorder is None:
            return self._answer(question, top_k, history, **kwargs)
        metadata = {'question': question, 'top_k': top_k, 'history': history or [],
                    'allowed_sources': sorted(allowed_sources) if allowed_sources is not None else None,
                    'memory_context': memory_context, 'use_graph': self.use_graph,
                    'conversation_id': (trace_context or {}).get('conversation_id')}
        with self.run_recorder.capture(metadata) as observer:
            result = self._answer(question, top_k, history, observer=observer, **kwargs)
            notify(observer, 'answer.final', asdict(result))
            return result

    def _answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources=None,
    memory_context=None, observer=None, input_evidence=None, skill_ids=None, working_memory=None) -> Answer:
        question = question.strip()

        if not question:
            raise ValueError("问题不能为空")

        if top_k <= 0:
            raise ValueError("回答时 top_k 必须大于 0")

        skills = self.skill_registry.session(skill_ids, max_tokens=min(1600, max(0, (self.context_window - self.max_output_tokens) // 4))) if self.skill_registry else None
        if skill_ids and skills is None:
            raise ValueError('未配置技能目录')

        kb = self.knowledge_base
        if allowed_sources is not None and kb is not None:
            kb = kb.restrict_sources(allowed_sources)
        if observer is not None and kb is not None:
            image_index = getattr(kb, 'image_index', None)
            notify(observer, 'retrieval.config', {
                **{key: getattr(kb, key, None) for key in (
                    'retrieval_mode', 'semantic_weight', 'ann_threshold', 'embedding_id',
                    'chunking_strategy', 'chunk_size', 'overlap', 'max_chunk_size')},
                'reranker_enabled': self.reranker is not None,
                'image_embedding_id': image_index.identity if image_index is not None else None,
                'image_page_count': len(image_index.chunks) if image_index is not None else 0,
                'context_window': self.context_window, 'max_output_tokens': self.max_output_tokens,
            })
        if self.use_graph and kb is not None:
            from deepsearch_agent.research_graph import ResearchWorkflow
            tool_session = None
            if self.agent_settings is not None and self.agent_settings.enabled:
                memory_args = {'working_memory': working_memory} if working_memory is not None else {}
                if self.tool_session_factory is None:
                    from deepsearch_agent.tool_service import PaperToolSession
                    tool_session = PaperToolSession(kb, settings=self.agent_settings, observer=observer,
                                                    input_evidence=input_evidence, reranker=self.reranker,
                                                    **memory_args)
                else:
                    tool_session = self.tool_session_factory(kb, settings=self.agent_settings,
                                                            observer=observer, input_evidence=input_evidence,
                                                            **memory_args)
            result = ResearchWorkflow(
                self.client, kb,
                context_window=self.context_window,
                max_output_tokens=self.max_output_tokens,
                reranker=self.reranker,
                on_progress=on_progress,
                memory_context=memory_context,
                search_intent=self.search_intent,
                observer=observer,
                visual_evidence=self.visual_evidence,
                skills=skills,
                tool_session=tool_session,
                mcp_settings=self.mcp_settings,
            ).answer(question, top_k, history, input_evidence=input_evidence)
            if self.history_store is not None:
                self.history_store.append(question=question, answer=result)
            return result

        chunks: list[TextChunk] = list(input_evidence or [])

        if kb is not None:
            from deepsearch_agent.retrieval_pipeline import retrieve
            chunks.extend(retrieve(kb, question, top_k, query_expander=self.query_expander,
                                   reranker=self.reranker, history=history))

        if not chunks:
            result = Answer(
                text="当前知识库未检索到可用证据，无法根据资料回答。",
                sources=[],
            )
        else:
            skill_prompt = skills.prompt() if skills else ''
            included, context = self._build_context(question, chunks, history, skill_prompt=skill_prompt)

            prompt = build_qa_prompt(
                question=question,
                context=context,
                history=history,
            )
            prompt += skill_prompt

            visual_context = [{'source': c.source_path, 'visual_pages': c.visual_pages} for c in included]
            if any(c.visual_pages for c in included):
                if self.visual_evidence is None:
                    from deepsearch_agent.llm_gateway import ModelServiceError
                    raise ModelServiceError('请配置视觉模型后阅读图表或扫描页')
                text = self.visual_evidence.complete(prompt, visual_context, structured=False)
            else:
                text = self.client.complete(prompt)
            result = Answer(
                text=text,
                sources=[
                    chunk_to_source_reference(chunk)
                    for chunk in included
                ],
            )

        if skills is not None:
            result.skills_used = skills.used
            notify(observer, 'skills.loaded', {'skills': skills.used, 'warnings': skills.warnings})
        if self.history_store is not None:
            self.history_store.append(
                question=question,
                answer=result,
            )

        return result

    def _build_context(
        self,
        question: str,
        chunks: list[TextChunk],
        history: list[dict[str, str]] | None,
        *, skill_prompt: str = '',
    ) -> tuple[list[TextChunk], str]:
        """Greedily assemble chunks into context within the token budget.

        Returns (included_chunks, context_string).  Chunks that don't fit
        are skipped; only included chunks appear in sources so the model
        can't cite evidence it never saw.
        """
        overhead = build_qa_prompt(question=question, context=None, history=history)
        fixed_tokens = estimate_tokens(overhead) + estimate_tokens("本轮上下文：\n\n") + estimate_tokens(skill_prompt)
        margin = 128
        available = self.context_window - self.max_output_tokens - fixed_tokens - margin
        if available < 1:
            available = 1

        included: list[TextChunk] = []
        parts: list[str] = []
        used = 0
        image_refs = set()
        visual_settings = getattr(self.visual_evidence, 'settings', None)
        for chunk in chunks:
            refs = {(chunk.source_path, page) for page in chunk.visual_pages}
            if visual_settings is not None and len(image_refs | refs) > visual_settings.max_images:
                continue
            formatted = format_chunk_for_context(chunk)
            index = len(included) + 1
            piece = f"[{index}] {formatted}"
            piece_tokens = estimate_tokens(piece)
            if used + piece_tokens > available:
                continue
            parts.append(piece)
            included.append(chunk)
            image_refs.update(refs)
            used += piece_tokens

        if not included:
            # Degenerate budget: truncate the first chunk so the model
            # still sees at least one piece of evidence.
            chunk = chunks[0]
            piece = f"[1] {format_chunk_for_context(chunk)}"
            piece_tokens = estimate_tokens(piece)
            if piece_tokens > available:
                non_text = piece_tokens - estimate_tokens(chunk.text)
                truncated_text = truncate_text(chunk.text, max(available - non_text, 1))
                truncated_chunk = dc_replace(chunk, text=truncated_text, page_spans=[])
                piece = f"[1] {format_chunk_for_context(truncated_chunk)}"
            parts.append(piece)
            included.append(chunk)

        return included, "\n\n".join(parts)


def snapshot_answer_service(state):
    """Called under the index lock; the request owns its retrieval view afterward."""
    from copy import copy
    if not isinstance(state.answer_service, AnswerService):
        return state.answer_service
    service = copy(state.answer_service)
    kb = state.kb
    image_index = getattr(kb, 'image_index', None)
    sources = {c.source_path for c in kb.chunks}
    if image_index is not None:
        sources.update(c.source_path for c in image_index.chunks)
    service.knowledge_base = kb.restrict_sources(sources)
    return service
        
