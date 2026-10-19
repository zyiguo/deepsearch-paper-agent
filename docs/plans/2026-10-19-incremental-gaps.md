# Incremental gap verification implementation plan

Goal: retain unknown candidates while avoiding repeated semantic reviews that provide no new witnesses for previously verifier-reported missing requirements.

Architecture: opt-in v5 alongside unchanged v4 baseline. Standard-library core stores bounded gap records per stable candidate identity. Application graph filters only previously unknown paths lacking fresh gap witnesses; positive support always needs independent verification. This is a conservative abstention heuristic, not proof of semantic impossibility. New quotes, source content, question/requirement identity or workflow context invalidate reuse. All excluded paths and reasons remain traceable, candidates remain unknown.

1. Add core gap-state construction and delta checks, with tests for missing requirements, new witnesses, changed sources, malformed reports, alternative paths and unknown retention.
2. Add concise review schema: 280-character diagnostic reasons, 3 short issues and 3 short queries. Truncate only diagnostic reasons locally after raw-response tracing; never truncate verdicts, indices or supported requirements. Preserve strict structural validation.
3. Integrate v5 generation gap hints, review filtering, checkpoint fields, context budgeting and explicit opt-in configuration. Record review reuse separately from actual semantic review.
4. Test actual graph and full suite using short project-local temporary paths. Freeze source and run same original four cases, same model/corpus/budgets, v4 versus v5. No automatic retries; preserve prior artifacts by checksum.
5. Report actual provider Token totals, role costs, quality, unknown outcomes, gap reuse counts and limitations. Preserve null human false-prune rate without independent labels.
