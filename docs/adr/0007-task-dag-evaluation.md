# ADR-0007: Declarative task-DAG evaluation

Status: Implemented as an opt-in offline evaluator, 2026-10-07.

## Context

Existing paired ChartQA/PDFVQA experiments record final answers and detailed
retrieval/tool/context traces. Flat stage metrics do not encode required
prerequisites or distinguish independent failures from blocked downstream goals.
Existing dataset labels do not include intermediate calculation targets.

## Decision

Use a versioned, evaluator-only JSON sidecar with Pydantic validation and
Python's graphlib topological sort. Keep pure rule evaluation separate from
the recorded-run adapter and report generation. Allow AND/OR dependencies,
partial numeric/set credit, required goals, zero-weight diagnostics, and
explicit unknown observations. Preserve the original dataset metrics.

Automatically generated graphs are labeled stage_proxy. Task-specific rubrics
require separately annotated intermediate outputs. Evaluate dependencies as
logical score prerequisites, not mandatory chronological tool sequences. Do
not expose rubric answers to agents, evaluate arbitrary expressions, infer
private reasoning, or use model self-verification as ground-truth correctness.

## Alternatives and consequences

Strict trajectory matching would penalize valid alternative plans. Converting
the runtime LangGraph into a DAG would remove useful iterative search. An LLM
judge introduces cost and calibration requirements before there are reliable
task labels. The selected design reuses immutable local traces at zero model
cost, at the expense of leaving unobserved intermediate artifacts unknown.

Weights and dependency annotations can bias scores. Reports retain raw node
scores, dependency-gated scores, observation coverage, original metrics, and
annotation provenance. A dependency failure frontier is not a proven causal
root. Fingerprints bind rubric, case, source predictions, indices and evaluator
code. Cycles, duplicate nodes, missing dependencies and incompatible metrics
fail validation. Historical outputs are not overwritten.

The first acceptance replay uses 12 existing cases and 60 ordinary trajectories;
12 given-image oracle trajectories remain separate. This verifies the pipeline,
not benchmark validity, generalization, or gains from changing model behavior.
