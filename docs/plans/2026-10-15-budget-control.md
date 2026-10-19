# Budget Control Implementation Plan

**Goal:** Compare unchanged mainline, visible budgets, and visible budgets with bounded research stopping without increasing shared limits.

**Architecture:** Inject an optional label-free controller into the real research graph. The benchmark runtime supplies resource counters; admitted search chunk IDs supply a novelty proxy. Default application behavior stays unchanged. Only Supervisor and Researcher see budgets, never the Verifier.

**Tech Stack:** Python, LangGraph, existing benchmark runtime, pytest.

## Tasks

1. Add `budget_control.py`: immutable policy, sanitized budget snapshots, admitted-chunk novelty, latched research-stop reasons. No gold or document contents.
2. Add optional controller hooks to `research_graph.py` and `agentic_research.py`. Stop before planning/tool dispatch; finalize through existing Answerer and Verifier. No automatic support or citation approval.
3. Register `mainline-budget-visible` and `mainline-budget-bounded` in the benchmark runner, preserving `mainline` as the control. Hash controller and method policy in run identity.
4. Test resource visibility, search ceiling, model-call reserve, no-progress stopping, partial/unsupported answers, default behavior and verifier isolation.
5. Run full regression, then six independent cloud canaries (two datasets x three arms) with the existing limits. Preserve failures and costs; no retries/tuning within the batch.
6. Report mechanics separately from efficacy. The paired 10-question-per-dataset, three-arm pilot is a subsequent experiment, not a conclusion inferred from canaries.

## Frozen Initial Policy

- Five model calls reserved for finalization (draft, verify, possible repair, supervisory routing, projection). Conditional graph routing can use fewer.
- Stop new research at zero remaining searches, five remaining model calls, remaining accounted tokens <= five output allowances, or <=30 seconds remaining.
- Stop after two consecutive searches with no newly admitted chunk IDs. This is not semantic evidence sufficiency; valid evidence may already be present or still missing.
- Token/time thresholds are heuristics, not completion guarantees. Runtime hard limits remain authoritative, including finalization calls.
- Once research stops, existing evidence can be drafted and independently verified; failed verification remains incomplete or abstains. No extra precision requirements in the budget guidance beyond the original question.
- Default mainline prompts/routing are unchanged when the controller is absent. Budget-visible shares the same guidance but does not enforce the stop rules.
