# Mainline Multi-hop Comparison

## Scope

Run the real application `ResearchWorkflow`, including `AgenticResearcher` and
`PaperToolSession`, on the exact two frozen pilot manifests used by V2 baselines.
This isolates orchestration under BM25, not the full deployed hybrid/multimodal stack.

## Controls

- Model: `mimo-v2.6-pro`, existing endpoint, direct network, default provider thinking.
- Per question: 24 model calls, 8 searches, k=5, 256000 cumulative token budget,
  8192 output tokens, 131072 context, 600 seconds; HTTP timeout remains 300 seconds.
- Every role, schema repair, and final short-answer projection uses the same runtime.
- The short-answer projection sees only the question and verified claim text, no
  additional retrieval or evaluator labels. Preserve the original verified answer.
- Retain graph defaults: 8 dispatches, 3 research rounds, 3 reviews, one schema repair,
  18 total tool calls, 30 admitted evidence chunks; 6 planner steps per round.
- Expose only search and reading already-retrieved evidence. No application state,
  Wiki, working memory, external MCP, skills, images, embeddings, or reranking.
- Source-scoped search filters the common top-k results; it cannot construct an
  alternate index or retrieve extra documents for free.
- Mainline structured calls use the common generation policy, not the deployment
  gateway's structured mode that disables MiMo thinking. Local graph validation remains.

## Execution

1. Offline graph, budget, label-isolation and historical-comparison tests.
2. Independent one-question cloud canary for each dataset.
3. Ten-question pilot per dataset, same manifests; no automatic retries or tuning.
4. Offline rescore, explicit historical-reference comparison, case-level attribution.

V2 references remain immutable. The pre-change fingerprint in
`data/benchmarks/multihop/runs/mainline-20261008-prechange.json` binds the old code
and result files. Mainline necessarily has a new code identity. Historical comparison
still rejects different manifests, model settings, retrieval settings and budgets;
it explicitly does not claim same-build equivalence. Preserve the V2 MuSiQue/IRCoT
interruption caveat: pre-interruption unpersisted cloud usage is unknown.

## Interpretation

This is a small, already-inspected development pilot, not a held-out statistical
evaluation or official paper reproduction. EM/F1 include execution failures as zero.
Document support recall is cumulative document-level proxy, not Recall@5. Mainline
verification is a model judgment, not a correctness oracle. Budget ceilings match;
algorithm-specific iterations and auxiliary-call allocation are intentionally different.
