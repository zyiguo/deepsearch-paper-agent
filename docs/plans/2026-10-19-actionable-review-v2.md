# Actionable review and revisable assumptions v2

Goal: address repeated revision and prematurely fixed entity interpretations from the saved v1 badcases without changing mainline/v1 semantics or human labels.

1. Introduce a distinct v2 policy and method identity. Add optional production version selection, still defaulting to disabled/v1.
2. Add typed verifier findings with target requirements/claims, a blocking flag, and an executable action. Notes cannot downgrade legacy free-text issues, bad references, missing coverage or rejected claims.
3. Cache the last verifier input/result in graph state. Only exact canonical question/evidence/claim/requirement/assumption input equality permits reuse. Reuse plus remaining blockers terminates repetition; new evidence or changed claims invalidates it. No semantic-equivalence claim.
4. Separate frozen question requirements from initially unconfirmed assumptions. Only independent verifier updates with authorized citations and exact supporting quotations can change assumptions. Save old/new values and invalidate verification after a change.
5. Route research/revise from typed findings under existing resource/review/dispatch limits. Finish only after all claims, coverage, references and blockers pass. Preserve nonblocking notes separately.
6. Add graph-level tests for notes versus genuine conflicts, repeated revisions, cache invalidation, assumption corrections, authorization, checkpoint recovery and legacy compatibility. Replay saved badcases offline with explicit adaptation labels.
7. Run complete backend and architecture checks, verify old evaluation artifacts remain unchanged, document validated scope. No automatic promotion, gold rewrites or retuning saved failures.
