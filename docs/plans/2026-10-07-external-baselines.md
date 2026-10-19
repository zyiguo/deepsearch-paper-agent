# External Multi-hop Baselines Implementation Plan

**Goal:** Evaluate IRCoT-adapted and LogicRAG-adapted on author-released MuSiQue and 2Wiki data with isolated labels, reproducible identities, bounded calls, and offline scoring.

**Architecture:** Add `benchmarks/multihop` alongside the existing multimodal evaluator. Dataset import, method adapters, retrieval/model runtime, and scoring have separate contracts. Existing application conversations, Wiki, memory, and multimodal evaluation remain unchanged.

**Tech Stack:** Python 3.12, Pydantic 2, existing BM25 and OpenAI-compatible gateway, pytest, optional lightweight sentence segmentation.

## Scope and Claims

- Import author data rather than invent a new benchmark. IRCoT processed records require a separately supplied corpus. LogicRAG's released candidate corpus remains explicitly labeled `sampled_pool`.
- Preserve author question IDs, source revision, source file hashes, corpus identity, and deterministic sampling. A hash preserves identity; it does not certify correctness or prove an official corpus.
- Model inputs contain only questions and retrieved documents. Answers/support labels are held by the evaluator.
- Both baselines are adaptations, not paper-score reproductions. Changing the model, retriever, segmentation, or failure handling must appear in provenance.
- IRCoT released prompt assets stay in ignored local data, selected independently of evaluation labels. No fabricated gold DAGs.
- First deliverable is author-data import plus an offline-tested execution/scoring harness. Cloud benchmarks require explicit CLI opt-in; no performance claim from mock tests.

## Structure

```text
src/deepsearch_agent/benchmarks/multihop/
  schema.py       # Agent-visible documents/questions and evaluator-only gold
  datasets.py     # Explicit author-format import and fingerprints
  upstream.py     # Pinned, bounded downloads; scripts are never executed
  baselines.py    # IRCoT/LogicRAG method contracts and adaptation metadata
  runtime.py      # Shared BM25, model budgets, traces, usage accounting
  runner.py       # Identity-checked resume and per-case durable results
  metrics.py      # Answer EM/F1 and named support-recall diagnostic
  __main__.py     # fetch, import, inspect, run, score
docs/MULTIHOP_BASELINES.md
tests/test_multihop_*.py
data/benchmarks/multihop/    # Ignored downloaded assets and prepared manifests
artifacts/multihop/         # Ignored runs and reports
```

## Task 1: Author Data and Metrics

1. Inspect author record schemas, sampling scripts and evaluation semantics.
2. Write fixtures for real field layouts, invalid IDs, missing support, duplicates and label separation.
3. Implement strict import, source hashing and seeded sampling; never synthesize a full corpus from selected gold context.
4. Test answer normalization/aliases and missing-prediction denominators. Document unsupported official metrics.

## Task 2: External Methods

1. Pin upstream commits and inspect license notices and actual control flow.
2. Isolate method logic behind `generate` and `search` interfaces. Do not add our proposed condition/gating improvements to baselines.
3. Use author IRCoT prompt assets and record selection; retain final QA generation.
4. Test query sequence, stop behavior, malformed output and budget-exception propagation.

## Task 3: Runtime and Persistence

1. Reuse project BM25 with explicit metadata that it differs from author Elasticsearch/dense retrieval.
2. Enforce per-case search/model-call limits and preflight token estimates; report unknown provider usage as unknown, not zero.
3. Save per-case traces and immutable run identity. Resume only identical data, prompts, adapter code and configuration. Failed attempts remain visible.
4. Require `--allow-remote`; secrets never enter logs or identity files. Network download and cloud-call permissions are separate.

## Task 4: Verification and Documentation

1. Run ` .venv/Scripts/python.exe -m pytest tests/test_multihop_data.py tests/test_multihop_baselines.py tests/test_multihop_upstream.py tests/test_multihop_runtime.py `.
2. Fetch bounded author reference/data files through the requested download proxy; run actual import/inspect without cloud generation.
3. Exercise offline end-to-end run, scoring, interrupted resume and incompatible-resume rejection.
4. Run relevant existing benchmark tests and `git diff --check`; preserve unrelated dirty files.
5. Document exact commands, current limitations, original-vs-adapted semantics, and the next cloud pilot. Do not commit/push unrelated work.

## Deferred

- Live-web Chinese benchmarks and long-report adaptation.
- New rule extraction, calibrated pruning, injection gates and their ablations.
- Human-reviewed reasoning DAG annotations, official MuSiQue-Full group sufficiency, and a claim of reproducing published scores.
- Large corpus downloads, hosted model evaluation and statistics beyond an explicitly approved pilot.

## First Increment Status

Implemented and tested the new package, two source-checked method adaptations,
author-format imports, pinned bounded asset downloads, shared runtime, resumable
attempt records, offline scoring and same-condition comparison. Imported actual
LogicRAG author data into two 10-question pilot manifests without reducing the
released corpus. Full repository verification: 641 passed, 2 skipped (2026-10-07).
See `docs/MULTIHOP_BASELINES.md` for source-data warnings and exact commands.
IRCoT full-corpus acquisition, live cloud validation and the application Agent
adapter remain follow-up work; no improvement claim has been evaluated yet.
