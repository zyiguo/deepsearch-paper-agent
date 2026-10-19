"""Bounded candidate generation and independent path-review wire contracts."""
from typing import Literal
from pydantic import Field, StrictInt, field_validator
from .contracts import Payload


class EvidenceStep(Payload):
    requirement_index: StrictInt = Field(ge=0, le=7)
    claim: str = Field(min_length=1, max_length=600)
    citation: StrictInt
    quote: str = Field(max_length=1200)


class EvidencePath(Payload):
    steps: list[EvidenceStep] = Field(max_length=12)


class Candidate(Payload):
    answer: str = Field(min_length=1, max_length=600)
    interpretation: str = Field(min_length=1, max_length=600)
    paths: list[EvidencePath] = Field(max_length=3)


class CandidateDraft(Payload):
    candidates: list[Candidate] = Field(max_length=4)


class CandidateUpdate(Candidate):
    existing_candidate_id: str | None = Field(default=None, max_length=64)


class CandidateUpdates(Payload):
    candidates: list[CandidateUpdate] = Field(max_length=4)


class PathReview(Payload):
    candidate_index: StrictInt = Field(ge=0, le=3)
    path_index: StrictInt = Field(ge=0, le=2)
    verdict: Literal['supported', 'contradicted', 'unknown']
    supported_requirements: list[StrictInt] = Field(max_length=8)
    reason: str = Field(min_length=1, max_length=1000)


class CandidateVerdict(Payload):
    paths: list[PathReview] = Field(max_length=12)
    selected_candidate: StrictInt | None
    answer_resolved: bool = Field(strict=True)
    issues: list[str] = Field(max_length=8)
    queries: list[str] = Field(max_length=3)


class ConcisePathReview(PathReview):
    reason: str = Field(min_length=1, max_length=280)

    @field_validator('reason', mode='before')
    @classmethod
    def bound_diagnostic(cls, value):
        # Raw response remains in trace. Only descriptive text is truncated;
        # decision-bearing fields retain strict validation and repair behavior.
        return value[:280] if isinstance(value, str) else value


class GapVerdict(CandidateVerdict):
    paths: list[ConcisePathReview] = Field(max_length=12)
