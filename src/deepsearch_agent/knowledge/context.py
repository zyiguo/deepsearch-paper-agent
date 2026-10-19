"""Question-answering service built on an LLMClient."""

from deepsearch_agent.core.answers import format_chunk_for_context

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.prompts import build_qa_prompt
from deepsearch_agent.core.tokens import estimate_tokens, truncate_text
from dataclasses import replace as dc_replace


class ContextAssembler:
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
