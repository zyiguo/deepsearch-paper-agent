"""Question-answering service built on an LLMClient."""

from deepsearch_agent.core.answers import Answer

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.ports import LLMClient, RunRecorder, EventSink
from deepsearch_agent.knowledge.query_expander import QueryExpander
from deepsearch_agent.knowledge.reranker import Reranker
from dataclasses import asdict
from deepsearch_tracing.context import notify
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepsearch_agent.core.ports import AnswerStore
    from deepsearch_agent.core.ports import VisualEvidenceProvider


class AuditedAnswerService:
    def __init__(
        self,
        client: LLMClient,
        knowledge_base: KnowledgeBase | None = None,
        history_store: "AnswerStore | None" = None,
        use_graph: bool = False,
        context_window: int = 8192,
        max_output_tokens: int = 1024,
        reranker: Reranker | None = None,
        query_expander: QueryExpander | None = None,
        search_intent=None,
        run_recorder: RunRecorder | None = None,
        visual_evidence: 'VisualEvidenceProvider | None' = None,
        skill_registry=None,
        agent_settings=None,
        tool_session_factory=None,
        mcp_settings=None,
        wiki_store=None,
        checkpointer=None,
        tool_ledger=None,
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
        self.wiki_store = wiki_store
        self.checkpointer = checkpointer
        self.tool_ledger = tool_ledger


    def answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources: set[str] | None = None,
    memory_context=None, trace_context=None, input_evidence: list[TextChunk] | None = None, skill_ids=None,
    working_memory=None, run_id=None, resume=False, event_sink: EventSink | None = None) -> Answer:
        kwargs = dict(on_progress=on_progress, allowed_sources=allowed_sources, memory_context=memory_context,
                      input_evidence=input_evidence, skill_ids=skill_ids, working_memory=working_memory,
                      run_id=run_id, resume=resume)
        if self.run_recorder is None:
            return self._answer(question, top_k, history, observer=event_sink, **kwargs)
        metadata = {'question': question, 'top_k': top_k, 'history': history or [],
                    'allowed_sources': sorted(allowed_sources) if allowed_sources is not None else None,
                    'memory_context': memory_context, 'use_graph': self.use_graph,
                    'conversation_id': (trace_context or {}).get('conversation_id'),
                    'research_run_id': run_id, 'resume': resume,
                    'runtime_identity': (trace_context or {}).get('runtime_identity'),
                    'corpus_identity': (trace_context or {}).get('corpus_identity')}
        with self.run_recorder.capture(metadata) as recorder_observer:
            def observer(kind, payload):
                notify(recorder_observer, kind, payload)
                notify(event_sink, kind, payload)
            result = self._answer(question, top_k, history, observer=observer, **kwargs)
            notify(observer, 'answer.final', asdict(result))
            return result


    def _prepare(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources=None,
    memory_context=None, observer=None, input_evidence=None, skill_ids=None, working_memory=None,
    run_id=None, resume=False) -> tuple:
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
        wiki_view = None
        if self.wiki_store is not None and kb is not None:
            from deepsearch_agent.workspace.llm_wiki import WikiView
            images = getattr(kb, 'image_index', None)
            registry = {c.chunk_id: c for c in [*kb.chunks, *(images.chunks if images else [])]}
            wiki_view = WikiView(self.wiki_store, {c.source_path for c in registry.values()}, registry)
        if observer is not None and kb is not None:
            from deepsearch_agent.core.identity import fingerprint as digest
            image_index = getattr(kb, 'image_index', None)
            notify(observer, 'retrieval.config', {
                **{key: getattr(kb, key, None) for key in (
                    'retrieval_mode', 'semantic_weight', 'ann_threshold', 'embedding_id',
                    'chunking_strategy', 'chunk_size', 'overlap', 'max_chunk_size')},
                'reranker_enabled': self.reranker is not None,
                'image_embedding_id': image_index.identity if image_index is not None else None,
                'image_page_count': len(image_index.chunks) if image_index is not None else 0,
                'context_window': self.context_window, 'max_output_tokens': self.max_output_tokens,
                'corpus_fingerprint': digest(sorted(c.chunk_id for c in [
                    *getattr(kb, 'chunks', []), *(image_index.chunks if image_index is not None else []),
                    *(input_evidence or []),
                ])) if hasattr(kb, 'chunks') else None,
            })
        return kb, skills, wiki_view

    def snapshot(self, kb):
        from copy import copy
        service = copy(self)
        images = getattr(kb, 'image_index', None)
        sources = {c.source_path for c in kb.chunks}
        if images is not None:
            sources.update(c.source_path for c in images.chunks)
        service.knowledge_base = kb.restrict_sources(sources)
        return service
