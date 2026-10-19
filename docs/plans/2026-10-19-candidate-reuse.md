# Candidate reuse experiment

Keep v3 unchanged as the same-build baseline; add opt-in v4. Preserve all unknown candidates and historical proof snapshots. Use explicit existing candidate IDs for updates, immutable answer/interpretation identities, conservative whitespace deduplication, and unique proof paths. Never merge homonyms using fuzzy similarity.

Compact previous candidates in generation prompts. Skip generation only when question, requirements, evidence, instruction and identity inputs are identical. Empty proof sets cannot support an answer: retain unknown and request requirements-focused research without a semantic verifier call. Any nonempty proof still requires independent verification. Record skipped calls and input identities; do not estimate provider tokens saved as actual usage.

Validate identity updates, unknown retention, unchanged-input stopping, changed-evidence reopening, and real workflow behavior. Run the existing four canary cases under identical original model/corpus/budgets with v3 and v4. Freeze source before inference, preserve errors and unknown usage, quarantine the existing invalid gold, and report actual total and role Token costs alongside quality.
