"""Bounded local-document research workflow; no tracing or checkpoint storage."""
from typing import TypedDict, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from deepsearch_agent.agents.subagent_contracts import ShortText


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Queries(Payload):
    queries: list[str] = Field(max_length=3)


class Claim(Payload):
    text: str = Field(min_length=1, max_length=3000)
    citations: list[StrictInt] = Field(min_length=1, max_length=20)


class Draft(Payload):
    claims: list[Claim] = Field(max_length=12)


class AnswerableOmission(Payload):
    question: str = Field(min_length=1, max_length=1000)
    citations: list[StrictInt] = Field(min_length=1, max_length=20)


class Verdict(Queries):
    supported: list[StrictInt] = Field(max_length=12)
    issues: list[str] = Field(default_factory=list, max_length=12)
    missing_answerable: list[AnswerableOmission] = Field(default_factory=list, max_length=12)


class ResearchTask(Payload):
    question: str = Field(min_length=1, max_length=1000)
    status: Literal['pending', 'in_progress', 'done', 'blocked']


class Decision(Payload):
    action: Literal['research', 'draft', 'revise', 'finish', 'stop']
    tasks: list[ResearchTask] = Field(max_length=8)
    instruction: str = Field(max_length=2000)
    skill_ids: list[str] = Field(default_factory=list, max_length=3)
    research_goal: str = Field(default='', max_length=4000)
    research_constraints: list[ShortText] = Field(default_factory=list, max_length=5)


class ControlledDecision(Decision):
    requirements: list[ShortText] = Field(min_length=1, max_length=8)
    answer_type: Literal['boolean', 'entity', 'number', 'date', 'list', 'other']


class RequirementCoverage(Payload):
    requirement_index: StrictInt = Field(ge=0, le=7)
    status: Literal['supported', 'partial', 'missing']
    citations: list[StrictInt] = Field(max_length=20)


class CoverageVerdict(Verdict):
    coverage: list[RequirementCoverage] = Field(max_length=8)


class PlanAssumption(Payload):
    key: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    statement: ShortText


class RevisableDecision(ControlledDecision):
    assumptions: list[PlanAssumption] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def unique_assumptions(self):
        if len({x.key for x in self.assumptions}) != len(self.assumptions):
            raise ValueError('Assumption keys must be unique')
        return self


class ReviewFinding(Payload):
    kind: Literal['missing_evidence', 'contradiction', 'citation_error', 'evidence_conflict', 'note']
    message: ShortText
    requirement_indices: list[StrictInt] = Field(max_length=8)
    claim_indices: list[StrictInt] = Field(max_length=12)
    blocking: bool = Field(strict=True)
    action: Literal['research', 'revise', 'finish', 'stop']

    @model_validator(mode='after')
    def consistent_action(self):
        allowed = {'missing_evidence': {'research', 'stop'}, 'contradiction': {'revise', 'stop'},
                   'citation_error': {'revise', 'stop'}, 'evidence_conflict': {'research', 'stop'},
                   'note': {'finish'}}
        if self.action not in allowed[self.kind] or self.blocking != (self.kind != 'note'):
            raise ValueError('Review kind, action and blocking flag disagree')
        return self


class AssumptionUpdate(PlanAssumption):
    status: Literal['supported', 'rejected']
    citations: list[StrictInt] = Field(min_length=1, max_length=8)
    quote: ShortText


class ActionableVerdict(CoverageVerdict):
    findings: list[ReviewFinding] = Field(max_length=12)
    assumption_updates: list[AssumptionUpdate] = Field(default_factory=list, max_length=8)


class ResearchState(TypedDict, total=False):
    question: str
    history: list
    top_k: int
    rounds: int
    evidence: list
    input_evidence: list
    claims: list
    supported: list
    queries: list
    failed: bool
    tasks: list
    instruction: str
    action: str
    issues: list
    reviews: int
    dispatches: int
    verified: bool
    stopped: bool
    missing_answerable: list
    stop_reason: str
    citation_errors: list
    subtask_results: list
    research_goal: str
    research_constraints: list
    parent_call_id: str
    requirements: list[str]
    answer_type: str
    coverage: list[dict]
    coverage_complete: bool
    review_notes: list[str]
    review_findings: list[dict]
    review_action: str
    review_cache: dict
    review_stalled: bool
    plan_assumptions: list[dict]
    plan_revision_pending: bool
    candidates: list[dict]
    candidate_evidence: list[dict]
    candidate_history: list[dict]
    candidate_generation_key: str
    candidate_generation_reused: bool
    verification_gaps: dict
    verification_gap_scope: str
    selected_candidate: int | None
