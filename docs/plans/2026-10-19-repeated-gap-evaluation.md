# Expanded repeated gap evaluation

Goal: measure natural gap-reuse activation, potential false deferral and repeated Token-cost stability without changing production policy during evaluation.

1. Freeze existing v4/v5 source and public model/corpus/budget identities. Preserve previous artifacts by hashes. Select 20 unique cases (10 per existing dataset), retaining four original canaries and choosing eight additional cases per dataset by stable SHA-256 ordering. Report original canaries and new cases separately. Existing invalid KABG gold remains excluded only from correctness metrics.
2. Run each case under v4/v5 three times, 120 total predictions. Each repeat has a new run directory; no retry of failed attempts. Keep concurrency four, same per-question budgets. Interleave version/repeat job order. Persist completed attempts and safe resume state.
3. Pair by dataset/question/repeat. Report actual total Token including failures, unknown usage separately, per-repeat deltas, per-question mean paired differences, win fractions and deterministic question-cluster bootstrap intervals. Three repetitions do not establish broad robustness.
4. Export every naturally deferred path with prior gap, current actual evidence, candidate and trace identity. Final answer exact-match is only a risk proxy, not a semantic false-deferral label. Independently replay deferred inputs through the verifier if natural events exist, recording extra audit costs separately; model disagreement is not human truth.
5. If natural trigger count is zero, report it explicitly and leave false-deferral rate undefined. Deterministic stress tests/replays remain separate from natural-run activation. Do not fabricate success or zero error from an empty denominator.
6. Deliver immutable plan, attempts, outcome groups, paired metrics, activation/audit queue, stability analysis and limitations. No production default changes.
