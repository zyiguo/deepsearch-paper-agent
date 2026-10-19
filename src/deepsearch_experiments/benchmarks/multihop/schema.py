"""Versioned, offline multi-hop benchmark contracts.

Only AgentCase is passed to a method; answers and supporting IDs stay in GoldCase.
"""

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DatasetName = Literal['musique', '2wikimultihopqa']
CorpusScope = Literal['full', 'external', 'sampled_pool']
AUTHOR_CORPUS_COUNTS = {'musique': 139416, '2wikimultihopqa': 430225}


def canonical_sha256(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def document_id(title: str, text: str) -> str:
    """Content addressing includes title and paragraph text, not source row order."""
    return 'doc_' + canonical_sha256([title.strip(), text.strip()])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class AgentCase(StrictModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)

    @field_validator('id', 'question')
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Case ID and question must not be blank')
        return value


class Document(StrictModel):
    id: str = Field(pattern=r'^doc_[a-f0-9]{64}$')
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @model_validator(mode='after')
    def content_identity(self) -> Self:
        if not self.title.strip() or not self.text.strip():
            raise ValueError('Document title/text must not be blank')
        if self.id != document_id(self.title, self.text):
            raise ValueError('Document ID does not match its content')
        return self


class GoldCase(StrictModel):
    id: str = Field(min_length=1)
    answers: list[str] = Field(min_length=1)
    support_document_ids: list[str] = Field(min_length=1)
    case_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('answers', 'support_document_ids')
    @classmethod
    def nonempty_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError('Gold values must be nonblank and unique')
        return values


class SourceFile(StrictModel):
    kind: Literal['questions', 'corpus', 'aliases']
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class BenchmarkManifest(StrictModel):
    schema_version: Literal[1] = 1
    dataset: DatasetName
    split: str = Field(min_length=1)
    protocol: Literal['ircot-processed-v1', 'logicrag-released-v1'] = 'ircot-processed-v1'
    source_revision: str = Field(pattern=r'^[a-f0-9]{40}$')
    corpus_scope: CorpusScope
    sources: list[SourceFile] = Field(min_length=2)
    seed: int = 0
    limit: int | None = Field(default=None, gt=0)
    available_cases: int = Field(gt=0)
    annotation_warnings: list[str] = Field(default_factory=list)
    cases: list[AgentCase] = Field(min_length=1)
    gold: list[GoldCase] = Field(min_length=1)
    documents: list[Document] = Field(min_length=1)

    @model_validator(mode='after')
    def check_alignment(self) -> Self:
        if not self.split.strip():
            raise ValueError('Split must not be blank')
        case_ids = [case.id for case in self.cases]
        gold_ids = [case.id for case in self.gold]
        document_ids = [doc.id for doc in self.documents]
        for kind, values in [('case', case_ids), ('gold', gold_ids), ('document', document_ids)]:
            if len(set(values)) != len(values):
                raise ValueError(f'Duplicate {kind} IDs')
        if set(case_ids) != set(gold_ids):
            raise ValueError('Case and gold IDs differ')
        if self.available_cases < len(self.cases):
            raise ValueError('Selected cases exceed available cases')
        expected_size = self.available_cases if self.limit is None else min(self.limit, self.available_cases)
        if len(self.cases) != expected_size:
            raise ValueError('Selected case count does not match sampling metadata')
        kinds = [source.kind for source in self.sources]
        if len(set(kinds)) != len(kinds) or not {'questions', 'corpus'} <= set(kinds):
            raise ValueError('Exactly one questions/corpus source and at most one aliases source required')
        if self.corpus_scope == 'full' and len(self.documents) != AUTHOR_CORPUS_COUNTS[self.dataset]:
            raise ValueError('Full corpus must match the author-reported document count; use external/sampled_pool otherwise')
        if self.protocol == 'logicrag-released-v1' and self.corpus_scope != 'sampled_pool':
            raise ValueError('LogicRAG released corpus must remain sampled_pool')
        document_set = set(document_ids)
        cases = {case.id: case for case in self.cases}
        for gold in self.gold:
            if not set(gold.support_document_ids) <= document_set:
                raise ValueError(f'Gold documents missing from corpus for {gold.id}')
            expected = case_fingerprint(self.dataset, self.split, cases[gold.id], gold.answers, gold.support_document_ids)
            if gold.case_sha256 != expected:
                raise ValueError(f'Case fingerprint mismatch for {gold.id}')
        return self


class Prediction(StrictModel):
    answer: str = ''
    retrieved_document_ids: list[str] = Field(default_factory=list)
    status: Literal['ok', 'error', 'cancelled', 'budget_exhausted', 'invalid_output', 'interrupted'] = 'ok'

    @field_validator('retrieved_document_ids')
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError('Retrieved document IDs must be nonblank and unique')
        return values


def case_fingerprint(dataset: str, split: str, case: AgentCase, answers: list[str], support_ids: list[str]) -> str:
    return canonical_sha256({
        'dataset': dataset, 'split': split, 'case': case.model_dump(),
        'answers': sorted(answers), 'support_document_ids': sorted(support_ids),
    })


def manifest_sha256(manifest: BenchmarkManifest) -> str:
    return canonical_sha256(manifest.model_dump(mode='json'))
