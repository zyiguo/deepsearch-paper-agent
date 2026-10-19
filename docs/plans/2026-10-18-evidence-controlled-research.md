# Evidence-controlled research implementation plan

**Goal:** Reduce research-only budget failures and protect answer types using an independently versioned, paper-informed inference policy.

**Architecture:** Keep the current graph and strict evidence authorization. Add an opt-in policy, per-role repair budgets, question-only requirements, and an independent verifier coverage ledger. Resource exhaustion ends research, never certifies truth. Keep the original mainline selectable with its original semantics; introduce a new method identity for comparisons.

**Tech stack:** Python dataclasses, Pydantic contracts, existing LangGraph workflow and offline evaluation runtime.

## Sources and adaptation boundary

- HALT, 2026-08-03, https://arxiv.org/html/2608.02009v1 : coverage of necessary hops controls stopping. We use the existing independent role, not the paper's trained Match/Partial/Null verifier. Requirements come from the question, never gold.
- Inference-Time Budget Control for LLM Search Agents, 2026-05-07, https://arxiv.org/html/2605.05701v1 : separate resource allocation from conservative final answer selection. We do not reproduce its tree search, critic or VOI formula.
- AnySearch, 2026-09-01, https://arxiv.org/abs/2609.00813 : requires reinforcement learning; excluded from this implementation because no trained policy is available here.

## Tasks

1. Add meaningful tests for missing bridge coverage, forged coverage citations, independent schema repairs, strict rejection, answer type preservation and unchanged legacy mainline.
2. Add `agents/evidence_policy.py` and extended contracts. Keep policy configuration separate from data and labels. Add state fields without rewriting old checkpoints.
3. Connect policy to coordinator, researcher and verifier: draft after child research, stop only on validated coverage, preserve uncertainty at budget boundaries. Bound each child by two tool calls as an explicit probe cadence, not an evidence-sufficiency claim.
4. Add application opt-in configuration and session resource control. Existing bounded benchmark control still enforces the original global budgets. No extra paid models or learned classifier dependencies.
5. Add `mainline-evidence-controlled-v1` with typed answer projection; every generation remains counted. Keep baseline generation/projection unchanged.
6. Run targeted tests, architecture and full backend regression. Use saved real responses for static regressions; distinguish these from new real inference. Record limitations and deployment flag in a final report.

## Acceptance and data protection

- A child cannot certify a claim, and neither a completed task nor exhausted budget bypasses Verifier.
- Coverage requires every question-derived requirement, unique valid requirement indices and authorized evidence citations; unknown coverage remains incomplete.
- Repair quotas are one per role, four maximum, inside the external call/token/time limits. No silent stripping of fields.
- Existing manifests, runs, scores and traces remain byte-identical. No automatic gold or human verdict changes.
- Production opt-in defaults off pending real paired evaluation; the new CLI method is explicit and source-fingerprinted.
- This is an engineering adaptation, not a reproduction or a demonstrated accuracy gain.
