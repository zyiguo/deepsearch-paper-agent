# PostgreSQL business database implementation plan

**Goal:** Add a real optional PostgreSQL business store for identity, workspace membership, conversations, memory and research run metadata, with explicit migration and preserved local SQLite operation.

**Architecture:** Database choice belongs to resource assembly. PostgreSQL adapters implement the same business store operations; a single bounded pool is shared by all workspace runtimes and closed after workers stop. Workspace business tables have composite keys, foreign keys, explicit scope predicates and forced row-level security with transaction-local scope. Identity authorization stays in the control plane. Indexes, files, Wiki Markdown, LangGraph checkpoints and the standard-library trace store remain independent.

**Tech Stack:** PostgreSQL 17 container, psycopg 3 and its bounded connection pool, existing standard-library SQLite backend. No ORM is required for the small explicit repository surface.

## Decisions

- Retain SQLite as the default. No automatic migration, fallback to SQLite on PostgreSQL failure, or dual writes. Reject unknown configuration, absent/invalid schema versions and privileged roles bypassing RLS.
- Explicit upgrade command applies checksum-bound schema migrations under a transaction/advisory lock. Runtime startup only validates an already upgraded schema.
- A local deployment has scope `local`; team deployments use server-generated workspace IDs. Every workspace transaction sets scope with SET LOCAL semantics so a reused pooled connection cannot inherit another workspace.
- JSON values use JSONB in PostgreSQL; public response objects, evidence IDs, session UUIDs, legacy turn IDs and memory revisions retain their meanings. Password hashes can be migrated; login sessions are deliberately excluded, requiring fresh login.
- Import SQLite sources read-only, reject nonempty destination scopes, validate source rows and foreign keys, preserve IDs, use one transaction per import and write a new report without overwriting. Existing user files remain unchanged. No automatic claim that restoring the old backend preserves writes made only in PostgreSQL.
- A separate Compose overlay opts into PostgreSQL, requires explicit credentials and uses a non-superuser application role. The existing Compose workflow continues to parse without database secrets.
- This does not enable multiple API replicas, distributed workers, pgvector retrieval or move the independent tracing database into production business tables.

## Steps

1. Extract public conversation memory operations from MemoryManager's SQLite SQL; keep legacy tests and contracts. Add config, pool/lifecycle and explicit schema migration modules under adapters.
2. Implement PostgreSQL identity, conversation/memory and research-run stores; reuse shared serializers/identity helpers and keep SQL in adapters. Inject through local/team startup and operator account CLI. Include safe database failure responses/readiness without leaking connection credentials.
3. Add explicit upgrade/status/import CLI with read-only source snapshots, conflict protection, dry-run and exclusive reports. Add opt-in Compose overlay/init script, dependency extra and configuration example.
4. Test SQLite regressions and real PostgreSQL: RLS/pool reuse, cross-scope keys, transactions/rollback, concurrent login and turn idempotency, memory CAS, authentication/membership, restart recovery and read-only imports. Exercise actual API containers with Redis/PostgreSQL, fail closed during database outage and verify recovery.
5. Update architecture ADR/operations and record exact validation results, resource cleanup and remaining limitations.
