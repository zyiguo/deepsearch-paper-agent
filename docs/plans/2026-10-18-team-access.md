# Team access implementation plan

**Goal:** Add an opt-in local account, workspace membership and resource isolation boundary for a small team, while preserving local single-user APIs and data.

**Architecture:** A team composition root authenticates opaque server sessions and authorizes every workspace request before dispatching the existing API to a workspace-owned runtime. Existing domain stores stay unchanged inside distinct server-generated workspace directories. Identity persistence, application policies, HTTP/session transport and UI remain separate.

**Tech Stack:** Python standard-library SQLite, scrypt and random tokens; existing FastAPI/ASGI, React and TypeScript. No new external service.

## Decisions and scope

- Keep the modular monolith and one API process. Use independent workspace stores rather than adding potentially incomplete row filters to every evidence/checkpoint/file path. A shared database with tenant filters is deferred until shared indexing and multi-process publication are designed.
- Local mode remains the default and keeps the frozen HTTP contract. Team mode is explicit and never imports existing local documents automatically.
- No public registration or default password. A password-prompt CLI creates the first system administrator. Administrators provision accounts; workspace owners add existing accounts as editor/viewer. System administration does not bypass workspace membership.
- Use server-generated immutable IDs, salted scrypt password hashes and hashed, expiring opaque session tokens in HttpOnly/SameSite=Strict cookies. Secure cookies default on. All mutations require an explicit request header and permitted Origin when present. Login is throttled by peer and account.
- Every business URL contains `/w/{workspace_id}/api/...`; the server checks current membership each request. No shared mutable active-workspace selector, so browser tabs cannot accidentally redirect each other's writes.
- Workspace ownership cannot be removed or transferred this round. Membership revocation blocks subsequent requests; already accepted tasks may finish in their original workspace. No claim of revoking bytes already downloaded or force-cancelling an active response.
- Runtime count is bounded; no unsafe eviction of active task resources. Shutdown drains work before closing per-workspace clients. Team mode rejects the unscoped MCP server until it has its own authorization transport.
- Authentication and membership events go to a separate identity audit table without passwords/tokens/content. Model/verifier/human labels remain separate.

## Implementation and checkpoints

1. Add `adapters/access_config.py`, `adapters/passwords.py`, `adapters/persistence/access.py`, `application/access.py` and a password-prompt CLI. Test password storage, expiry/revocation, membership and account throttling.
2. Add team composition in `application/bootstrap_team.py`, identity routes and an ASGI workspace boundary in `web/access.py`; extend `application/bootstrap.py` only at assembly. Scope cache namespaces and traces; reject unscoped business paths and team MCP. Test cross-workspace HTTP, files, memory, Wiki, run resume, viewer writes and startup/shutdown failures.
3. Add login/workspace/member/account UI under `frontend/src/features/access`; scope shared JSON, SSE, image and download URLs consistently. Keep credentials out of browser storage. Test URL binding, request headers and server rendering; build the frontend.
4. Add `.env.access.example`, ADR and operation/validation documentation. Run dependency audit, full backend/frontend regression and container smoke against synthetic accounts and new data only. Existing architecture fixtures stay unchanged for local mode.

## Acceptance

Anonymous access fails closed; foreign IDs cannot disclose or mutate resources; membership removal and logout invalidate future requests; viewer mutations are rejected; all persistent resources and cache namespaces differ between workspaces; old local files are untouched. Real cloud model correctness and multi-worker operation are outside this increment.
