"""Declarative, evaluator-only task DAGs. No executable expressions or model calls."""
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Spec(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)


class AnswerRule(Spec):
    kind: Literal['answer'] = 'answer'
    metric: Literal['primary', 'exact_match', 'relaxed_accuracy', 'accuracy',
                    'element_exact_match', 'element_f1'] = 'primary'


class SetRule(Spec):
    kind: Literal['set'] = 'set'
    field: Literal['evidence_ranking', 'context_ranking', 'ranking', 'original_image_pages',
                   'retrieved_element_ids', 'evidence_element_ids', 'context_element_ids']
    expected: list[str] = Field(min_length=1, max_length=1000)
    metric: Literal['recall', 'precision'] = 'recall'

    @model_validator(mode='after')
    def valid_expected(self):
        if len(set(self.expected)) != len(self.expected) or any(not x.strip() for x in self.expected):
            raise ValueError('Expected IDs must be unique and nonempty')
        return self


class ValueRule(Spec):
    kind: Literal['value'] = 'value'
    artifact: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,79}$')
    metric: Literal['exact', 'numeric', 'set_exact', 'set_f1'] = 'exact'
    expected: str | float | list[str]
    relative_tolerance: float = Field(default=0., ge=0., le=1.)
    absolute_tolerance: float = Field(default=0., ge=0.)

    @model_validator(mode='after')
    def valid_expected(self):
        if self.metric == 'numeric' and not isinstance(self.expected, float):
            raise ValueError('Numeric rules require a numeric expected value')
        if self.metric.startswith('set_'):
            if not isinstance(self.expected, list) or len(set(self.expected)) != len(self.expected):
                raise ValueError('Set rules require unique expected strings')
        if self.metric == 'exact' and not isinstance(self.expected, str):
            raise ValueError('Exact rules require an expected string')
        return self


class ToolRule(Spec):
    kind: Literal['tool'] = 'tool'
    names: list[str] = Field(min_length=1, max_length=30)
    expected_pages: list[str] = Field(default_factory=list)
    min_successes: int = Field(default=1, strict=True, ge=1, le=100)


class BudgetRule(Spec):
    kind: Literal['budget'] = 'budget'
    metric: Literal['model_calls', 'tool_calls', 'total_tokens', 'latency_ms']
    limit: float = Field(ge=0.)


class GateRule(Spec):
    kind: Literal['gate'] = 'gate'


Rule = Annotated[AnswerRule | SetRule | ValueRule | ToolRule | BudgetRule | GateRule,
                 Field(discriminator='kind')]


class TaskNode(Spec):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,79}$')
    description: str = Field(min_length=1, max_length=2000)
    depends_on: list[str] = Field(default_factory=list, max_length=128)
    dependency_mode: Literal['all', 'any'] = 'all'
    rule: Rule
    weight: float = Field(default=1., ge=0., le=1000.)
    threshold: float = Field(default=1., gt=0., le=1.)
    required: bool = True
    failure_tag: str = Field(default='task.unsatisfied', max_length=100)


class TaskDAG(Spec):
    case_id: str = Field(min_length=1)
    case_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    annotation_level: Literal['stage_proxy', 'task_rubric']
    annotation_status: Literal['generated', 'draft', 'reviewed'] = 'draft'
    reviewer: str | None = None
    nodes: list[TaskNode] = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def valid_graph(self):
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError('Duplicate DAG node IDs')
        if not any(node.required for node in self.nodes) or not sum(n.weight for n in self.nodes):
            raise ValueError('DAG needs required goals and positive total weight')
        if self.annotation_status == 'reviewed' and not (self.reviewer or '').strip():
            raise ValueError('Reviewed rubrics require reviewer provenance')
        for node in self.nodes:
            if len(set(node.depends_on)) != len(node.depends_on) or not set(node.depends_on) <= ids:
                raise ValueError('Duplicate or unknown dependency')
            if isinstance(node.rule, GateRule) and not node.depends_on:
                raise ValueError('A gate needs dependencies')
        try:
            self.ordered_nodes()
        except CycleError as error:
            raise ValueError('Task dependencies must form a DAG') from error
        return self

    def ordered_nodes(self):
        by_id = {node.id: node for node in self.nodes}
        return [by_id[key] for key in TopologicalSorter({n.id: n.depends_on for n in self.nodes}).static_order()]


class DAGSuite(Spec):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    manifests: dict[str, str]
    excluded_variants: list[str] = Field(default_factory=lambda: ['oracle'])
    tasks: list[TaskDAG] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_tasks(self):
        if len({task.case_id for task in self.tasks}) != len(self.tasks):
            raise ValueError('Duplicate DAG case IDs')
        return self


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
