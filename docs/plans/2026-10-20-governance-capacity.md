# Business Metrics, Quotas and Capacity Implementation Plan

> Execution: implement sequentially in this authorized shared workspace; preserve the preceding recovery increment and user files.

**Goal:** Deliver business metrics/alerts, atomic user/workspace daily cost quotas, deployment-wide resource protection and a reproducible capacity baseline.

**Architecture:** One shared governance service per API deployment, injected into workspace runtimes. A standard-library context propagates authenticated actor/workspace/run/operation identity into threads. Durable quota reservations and alert transitions use a separate SQLite control-plane database under the existing backed-up data root; PostgreSQL business storage stays unchanged. This matches the single-process deployment and does not add a queue or distributed coordination.

**Tech Stack:** Python standard library, existing FastAPI/httpx/SQLite; Prometheus text exposition without a new runtime dependency. Configuration defaults preserve existing API use: daily quotas unlimited until configured, bounded global concurrency enabled.

## Decisions and alternatives

- Choose one shared service rather than per-workspace counters: per-workspace counters cannot enforce one user's cross-workspace quota or deployment concurrency.
- Choose database transactions for quota reservation rather than Redis counters: unknown expenses must survive Redis loss and application restart. Persist reserved/running/settled/unknown/released states and an audited manual settlement route.
- Use internal integer cost units with configurable per-kind conversion; do not invent provider prices. Known token usage settles actual units; missing usage, timeout or interruption preserves reservation. Day rollover does not release old unknown expenses.
- Expose protected metrics and local alert states/logs. No outbound notifications or scheduled automation are created.

## Task 1: Shared contracts, configuration and control-plane persistence

Create `core/governance.py`, `adapters/governance_config.py`, `adapters/persistence/governance.py`. Add atomic dual-scope quota admission, daily UTC accounting, persistent unknowns, operator audit and explicit settings. Test concurrent pre-reservations, restart, midnight, unknown usage, overrun and failed persistence before connecting paid calls.

## Task 2: Metrics, alert states and resource limits

Create `application/governance.py`: bounded metric labels, counters/histograms, rolling business failure windows, alert pending/firing/resolved transitions, global/workspace/user resource leases. Test limits across workspaces, exception/cancellation release and sustained alert evaluation. Metrics failures do not break answers; accounting failures prevent paid requests.

## Task 3: Integrate actual execution boundaries

Inject one service through `bootstrap.py`/`bootstrap_team.py`; propagate trusted identity in `web/operations.py`, research threads and document executors. Record research business outcome independently of HTTP/SSE status. Instrument model gateway, text/image embeddings, Redis cache hits/misses and fallback, tool replay/unknowns, ingestion outcomes and phase durations. Admission denial before dispatch remains a definite non-execution.

## Task 4: Protected operations APIs

Create `web/governance.py` for metrics, alert states, current usage and admin quota/unknown-expense management. Team deployment totals/admin mutations require administrator permission; workspace usage requires membership and user details cannot expose another user's consumption. Keep CSRF/origin checks and actor attribution. Preserve previous OpenAPI snapshot with an explicit additions allowlist.

## Task 5: Capacity benchmark, docs and verification

Create an offline synthetic capacity runner with exclusive outputs, concurrency/latency/error/resource-peak measurements and memory measurement boundary. Exercise the production admission and quota services; add real ASGI integration tests for SSE business failure, background identity and multiple workspaces. Run backend suite, frontend checks if changed, architecture audit and synthetic fault/load validation. Save a new report and source fingerprints; do not overwrite earlier experiments or publish GitHub implicitly.

## Acceptance

- Two concurrent 8000-unit requests cannot both consume a remaining 10000-unit quota; user and workspace checks commit atomically.
- Unknown spending stays reserved across midnight and restart; replay of a saved tool result adds no provider charge.
- Workspace runtimes share the same capacity limits and actor accounting; actual workers preserve identity after HTTP disconnect.
- Metrics distinguish HTTP success, business completion, cancellation, interrupted runs, provider unknown usage and cached results; no prompts, secrets, user IDs or workspace IDs become metric labels.
- Alert state persistence and an operations read path work without external services; no false claim of delivered external notifications.
- Load reports state synthetic-provider and memory measurement limitations and preserve raw measurements in a new output directory.
