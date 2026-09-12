"""Query expansion protocol and LLM-based implementation.

A query expander rewrites the user's original question into alternative
phrasings better suited for document retrieval.  When conversation history
is available, the expander can resolve coreferences (e.g. "it", "this method")
to their referents from prior turns.
"""
import json
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deepsearch_agent.llm import LLMClient


class QueryExpander(Protocol):
    def expand(self, query: str, history: list[dict[str, str]] | None = None) -> list[str]: ...


class ExpandedQueries(BaseModel):
    model_config = ConfigDict(extra='forbid')
    queries: list[str] = Field(min_length=1, max_length=10)


class LLMQueryExpander:
    """Rewrites a query into alternative phrasings via one LLM call."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def expand(self, query: str, history: list[dict[str, str]] | None = None) -> list[str]:
        self.last_status = "running"
        instruction = (
            '你是一个检索查询改写器。将以下问题改写为3个不同的检索查询，'
            '用于在论文知识库中搜索相关片段。\n'
            '改写时使用更接近学术论文的措辞，补充可能相关的关键词和术语，'
            '如有历史对话则利用上下文消解指代（如"它""这个方法"等）。\n'
            '每个查询不超过50字。只返回 JSON：'
            '{"queries": ["改写1", "改写2", "改写3"]}\n\n'
        )
        history_part = ''
        if history:
            history_part = '历史对话：' + json.dumps(history[-6:], ensure_ascii=False) + '\n\n'
        prompt = instruction + history_part + f'问题：{query}'

        try:
            complete = getattr(self.client, 'complete_structured', self.client.complete)
            raw = complete(prompt).strip()
            if raw.startswith('```') and raw.endswith('```'):
                raw = re.sub(r'^```(?:json)?\s*', '', raw)[:-3].strip()
            result = ExpandedQueries.model_validate_json(raw)

            seen = {query.strip().lower()}
            expanded: list[str] = []
            for q in result.queries:
                q = q.strip()[:50]
                if q and q.lower() not in seen:
                    seen.add(q.lower())
                    expanded.append(q)
            self.last_status = "ok" if expanded else "empty"
            return expanded[:5]
        except (ValidationError, ValueError, KeyError, IndexError, RuntimeError):
            self.last_status = "fallback"
            return []
