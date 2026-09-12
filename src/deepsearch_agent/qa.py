"""Question-answering service built on an LLMClient."""

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.llm import LLMClient
from deepsearch_agent.prompts import SYSTEM_PROMPT, build_qa_prompt
from deepsearch_agent.query_expander import QueryExpander
from deepsearch_agent.reranker import Reranker
from deepsearch_agent.tokens import estimate_tokens, truncate_text
from dataclasses import dataclass, field, replace as dc_replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepsearch_agent.storage import JsonlAnswerStore
@dataclass
class SourceReference:
    source_path: str
    page_number: int
    text: str
    page_end: int | None = None
    section_path: list[str] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)


@dataclass
class Answer:
    text: str
    sources: list[SourceReference]

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
    ):
        self.client = client
        self.knowledge_base = knowledge_base
        self.history_store = history_store
        self.use_graph = use_graph
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.reranker = reranker
        self.query_expander = query_expander

    def answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources: set[str] | None = None, memory_context=None) -> Answer:
        question = question.strip()

        if not question:
            raise ValueError("问题不能为空")

        if top_k <= 0:
            raise ValueError("回答时 top_k 必须大于 0")

        kb = self.knowledge_base
        if allowed_sources is not None and kb is not None:
            kb = kb.restrict_sources(allowed_sources)
        if self.use_graph and kb is not None:
            from deepsearch_agent.research_graph import ResearchWorkflow
            result = ResearchWorkflow(
                self.client, kb,
                context_window=self.context_window,
                max_output_tokens=self.max_output_tokens,
                reranker=self.reranker,
                on_progress=on_progress,
                memory_context=memory_context,
            ).answer(question, top_k, history)
            if self.history_store is not None:
                self.history_store.append(question=question, answer=result)
            return result

        chunks: list[TextChunk] = []

        if kb is not None:
            from deepsearch_agent.retrieval_pipeline import retrieve
            chunks = retrieve(kb, question, top_k, query_expander=self.query_expander,
                              reranker=self.reranker, history=history)

        if not chunks:
            result = Answer(
                text="当前知识库未检索到可用证据，无法根据资料回答。",
                sources=[],
            )
        else:
            included, context = self._build_context(question, chunks, history)

            prompt = build_qa_prompt(
                question=question,
                context=context,
                history=history,
            )

            result = Answer(
                text=self.client.complete(prompt),
                sources=[
                    chunk_to_source_reference(chunk)
                    for chunk in included
                ],
            )

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
    ) -> tuple[list[TextChunk], str]:
        """Greedily assemble chunks into context within the token budget.

        Returns (included_chunks, context_string).  Chunks that don't fit
        are skipped; only included chunks appear in sources so the model
        can't cite evidence it never saw.
        """
        overhead = build_qa_prompt(question=question, context=None, history=history)
        fixed_tokens = estimate_tokens(overhead) + estimate_tokens("本轮上下文：\n\n")
        margin = 128
        available = self.context_window - self.max_output_tokens - fixed_tokens - margin
        if available < 1:
            available = 1

        included: list[TextChunk] = []
        parts: list[str] = []
        used = 0
        for chunk in chunks:
            formatted = format_chunk_for_context(chunk)
            index = len(included) + 1
            piece = f"[{index}] {formatted}"
            piece_tokens = estimate_tokens(piece)
            if used + piece_tokens > available:
                continue
            parts.append(piece)
            included.append(chunk)
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
        
