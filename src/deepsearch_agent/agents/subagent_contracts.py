"""Bounded task/result envelopes; private tool histories never cross this boundary."""
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
import json

from deepsearch_agent.core.tokens import estimate_tokens

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
EvidenceID = Annotated[str, StringConstraints(pattern=r'^chk_[a-f0-9]{64}$')]


class SubagentTask(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    task_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r'^[a-f0-9]{32}$')
    goal: str = Field(min_length=1, max_length=4000)
    instruction: str = Field(default='', max_length=2000)
    constraints: list[ShortText] = Field(default_factory=list, max_length=8)
    gaps: list[ShortText] = Field(default_factory=list, max_length=3)
    top_k: int = Field(default=5, ge=1, le=10)


class SubagentFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: ShortText
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=10)


class SubagentResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    status: Literal['completed', 'partial', 'blocked']
    findings: list[SubagentFinding] = Field(default_factory=list, max_length=6)
    evidence_ids: list[EvidenceID] = Field(default_factory=list, max_length=100)
    unresolved: list[ShortText] = Field(default_factory=list, max_length=8)
    stop_reason: str = Field(max_length=96)
    tool_calls: int = Field(ge=0)
    verification: Literal['unverified'] = 'unverified'


def compact_results(results, budget_tokens):
    """Give the coordinator bounded findings, preserving whole evidence IDs."""
    kept = []
    fits = lambda items: estimate_tokens(json.dumps(items, ensure_ascii=False)) <= budget_tokens
    for result in reversed(results[-3:]):
        item = {key: result[key] for key in ('task_id', 'status', 'stop_reason', 'verification')}
        item.update(evidence_count=len(result['evidence_ids']), unresolved_count=len(result['unresolved']),
                    findings=[], unresolved=[])
        if not fits([*kept, item]):
            break
        for finding in result['findings']:
            entry = {'text': finding['text'][:500], 'evidence_ids': finding['evidence_ids'][:3]}
            candidate = {**item, 'findings': [*item['findings'], entry]}
            if fits([*kept, candidate]):
                item = candidate
        for gap in result['unresolved'][:3]:
            candidate = {**item, 'unresolved': [*item['unresolved'], gap[:200]]}
            if fits([*kept, candidate]):
                item = candidate
        item['omitted_findings'] = len(result['findings']) - len(item['findings'])
        if not fits([*kept, item]):
            item.pop('omitted_findings')
        kept.append(item)
    return list(reversed(kept))
