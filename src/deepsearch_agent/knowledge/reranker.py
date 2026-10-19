"""Reranker protocol and LLM-based listwise implementation.

A reranker takes the candidate chunks returned by initial retrieval
(semantic / BM25 / hybrid) and re-orders them by a more precise
relevance signal — here, a single LLM call that sees all candidates
at once and returns a ranked ID list.
"""
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.ports import LLMClient
from deepsearch_agent.core.tokens import estimate_tokens, truncate_text
from deepsearch_agent.core.ports import VisualEvidenceProvider


class Reranker(Protocol):
    def rerank(self, query: str, chunks: list[TextChunk], top_k: int) -> list[TextChunk]: ...


class RerankResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ranked_ids: list[int] = Field(min_length=1)


class LLMReranker:
    """Listwise reranker: one LLM call ranks all candidates at once."""

    def __init__(
        self,
        client: LLMClient,
        context_window: int = 8192,
        max_output_tokens: int = 1024,
        visual_evidence: VisualEvidenceProvider | None = None,
    ) -> None:
        self.client = client
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.visual_evidence = visual_evidence

    def rerank(self, query: str, chunks: list[TextChunk], top_k: int) -> list[TextChunk]:
        self.last_status = "skipped"
        if len(chunks) <= 1:
            return chunks[:top_k]

        candidates = chunks[:20]
        use_vision = any(c.evidence_kind == 'image' for c in candidates)
        visual_settings = getattr(self.visual_evidence, 'settings', None) if use_vision else None
        if visual_settings is not None:
            bounded, pages = [], set()
            for candidate in candidates:
                refs = {(candidate.source_path, page) for page in candidate.visual_pages}
                if len(pages | refs) <= visual_settings.max_images:
                    bounded.append(candidate)
                    pages.update(refs)
            candidates = bounded
            if len(candidates) <= 1:
                return candidates[:top_k]

        instruction = (
            '你是一个检索结果重排序器。根据问题对文档片段按相关性从高到低排序。\n'
            '只返回 JSON：{"ranked_ids": [3, 1, 5, 2, 4]}\n'
            '编号从 1 开始，只包含给定编号，不增不减。\n\n'
        )
        query_part = f'问题：{query}\n\n'
        fixed = estimate_tokens(instruction) + estimate_tokens(query_part)
        margin = 256
        if visual_settings is not None and visual_settings.llm is not None:
            available = max(visual_settings.llm.context_window - visual_settings.llm.max_output_tokens
                            - len(pages) * visual_settings.image_token_budget - fixed - 1024, 200)
        else:
            available = max(self.context_window - self.max_output_tokens - fixed - margin, 200)

        total = sum(estimate_tokens(c.text) for c in candidates)
        if total > available:
            per_chunk = max(available // len(candidates), 50)
            texts = [truncate_text(c.text, per_chunk) for c in candidates]
        else:
            texts = [c.text for c in candidates]

        numbered = '\n\n'.join(
            (f'[{i}] source={c.source_path} page={c.page_number} kind={c.evidence_kind}\n{t}'
             if use_vision else f'[{i}] {t}')
            for i, (c, t) in enumerate(zip(candidates, texts, strict=True), 1))
        prompt = instruction + query_part + numbered

        try:
            if use_vision:
                if self.visual_evidence is None:
                    self.last_status = 'fallback_no_vision'
                    return candidates[:top_k]
                evidence = [{'id': str(i), 'source': c.source_path, 'visual_pages': c.visual_pages}
                            for i, c in enumerate(candidates, 1)]
                raw = self.visual_evidence.complete(prompt, evidence, structured=True).strip()
            else:
                complete = getattr(self.client, 'complete_structured', self.client.complete)
                raw = complete(prompt).strip()
            if raw.startswith('```') and raw.endswith('```'):
                raw = re.sub(r'^```(?:json)?\s*', '', raw)[:-3].strip()
            result = RerankResult.model_validate_json(raw)

            ids = result.ranked_ids
            expected = set(range(1, len(candidates) + 1))
            if set(ids) != expected or len(ids) != len(candidates):
                self.last_status = "fallback_invalid_ids"
                return candidates[:top_k]

            ranked = [candidates[i - 1] for i in ids]
            self.last_status = "ok"
            return ranked[:top_k]
        except (ValidationError, ValueError, KeyError, IndexError, RuntimeError):
            self.last_status = "fallback"
            return candidates[:top_k]
