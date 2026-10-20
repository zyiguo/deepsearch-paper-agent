# Redis Cache Protection Implementation Plan

**Goal:** Add bounded cross-request singleflight and TTL jitter to Embedding caching, preserve unknown paid-tool/cost recovery, verify against isolated real Redis, update documents and publish GitHub.

**Architecture:** Keep the single API process and the current model/credential cache identity. A CachedEmbeddingClient owns a bounded in-flight registry shared by its request snapshots. Atomic batch registration precedes computing newly owned keys; owners compute before waiting for overlapping keys. Followers never take over after timeout, cancel shared work, or retry a leader failure. TTL jitter only shortens the configured freshness upper bound. Durable tool and cost ledgers remain the recovery authority.

**Tech Stack:** Python standard-library locks/Future/ContextVar, redis-py, existing Governance and tool ledger, pytest, isolated Docker Redis, optional small synthetic-input real Embedding probes.

## Decisions and alternatives

- Use in-process request coalescing for the current deployment. A Redis lease would add expiry/failover ambiguity without making paid execution exactly-once. Multi-process coordination stays outside this change.
- Preserve batch computation of newly owned misses, deduplication, order and independent returned lists. Register all missing keys atomically, enforce capacity before dispatch, and avoid overlapping-batch deadlocks.
- Bounded follower wait and cancellation are separate from stopping provider work. In-flight keys remain owned until real computation completes. Exceptions fan out without automatic retries; no process-local cache result substitutes for a durable tool result.
- Default singleflight enabled, wait 65 seconds, maximum 4096 keys. Add independently configurable downward TTL jitter ratio 0.1; ratio 0 reproduces fixed TTL. TTL remains an integer from max(1, base-floor(base*ratio)) through base.

## Task 1: Implement and verify cache semantics

Modify `adapters/infrastructure.py`; extract an internal adapter helper if useful. Add validated settings and low-cardinality leader/follower/wait metrics. Add meaningful concurrency tests: hot-key expiry, overlapping batches, independent keys, input order, corruption, scope isolation, follower timeout/cancellation, leader exception, capacity and cleanup. Verify paid unknown and cost reservations with existing public ledger services.

## Task 2: Real Redis fault and concurrency runner

Create `scripts/verify_cache_protection.py` with exclusive output directories and random test namespace. Use only isolated service resources; never FLUSH a user database. Record actual provider dispatch counts, TTL distribution, shared wait outcomes, stop/restart degradation and recovery, cache write failures, stable-operation unknown recovery. Keep failures and missing usage explicit.

Use a local deterministic counted provider for concurrency/fault attribution. Optionally run a small real configured Embedding baseline/protected pair with synthetic inputs, no retries, bounded concurrency and cost accounting. Real Redis verification must not be replaced by a fake backend test.

## Task 3: Integration and regression

Update `.env.infrastructure.example` and any fixed-TTL fixture assumptions. Run focused tests first, then backend regression and architecture audit. Existing JSON/index/checkpoint formats, key hash, safe model diagnostics and backup behavior remain compatible. Record actual checks and environment; do not overwrite old reports or mutate user model configuration.

## Task 4: Documents and publication

Update current architecture, infrastructure, technical report, knowledge points and cache topic from design to implemented scope. Save a new dated validation report and sanitized public JSON with source hashes. Stage an explicit allowlist, audit content, commit/push non-force and verify remote bytes. Exclude runtime databases, credentials, user notes and training directories.
