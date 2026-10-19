# Tool Execution Ledger Implementation Plan

**Goal:** Persist logical tool operations across interruption, replay confirmed results, and retain uncertain paid/effectful outcomes as unknown.

**Architecture:** Standard-library core contracts; workspace-local SQLite adapter alongside existing checkpoints; application executor injected into tool sessions. Persist child planner decisions so a resumed node reuses the same logical slots instead of generating different calls. Default external tools to effectful; do not infer safety from model-selected annotations.

**Tech Stack:** Python, SQLite transactions, LangGraph adapters, existing FastAPI run API and pytest fault injection.

## Tasks

1. Add core operation kinds, recovery errors, result envelope and repository protocol. Add SQLite operations/events tables, unique run/slot identities, argument/policy validation, fenced execution claims and atomic result commits.
2. Implement sync/async executor with durable claimed/dispatched phases. Restart recovers claimed operations to pending and dispatched operations to unknown. Read-only unknown may retry; paid/effectful unknown requires provider reconciliation or a trusted idempotency capability. Never retry merely because of timeout.
3. Inject workspace ledger at startup and per-run executor into AnswerService. Reconcile only interrupted runs; clean up ledger records with session deletion. Add authorized read-only operation status endpoint without result payloads or arguments.
4. Persist planner action per research round/step and make task/call identity stable. Wrap local and external tools; preserve evidence registration, memory notes, logical budget accounting and unknown error propagation through ToolNode.
5. Fault-inject before dispatch, after remote effect before commit, after result commit before graph checkpoint; test same/different arguments, intentional repeated steps, concurrent claims, readonly replay, paid charging, side effects, provider idempotency/reconciliation, and API workspace isolation.
6. Run focused tests, architecture audit and full regression; document observed duplicate requests/effects/charges separately. No real provider exactly-once claim or mid-run modification of previous evaluation artifacts.

## Boundaries

Single API deployment remains the supported runtime. SQLite files are inside the existing workspace backup roots even with PostgreSQL business metadata. Unknown is a blocking safety state for non-readonly operations. Model HTTP calls outside the child planner and tool adapter are not covered by this increment. Old checkpoints without a matching ledger prefix cannot safely resume tool work and must be rejected. User has already authorized implementation; no separate design approval or automatic publication is required.
