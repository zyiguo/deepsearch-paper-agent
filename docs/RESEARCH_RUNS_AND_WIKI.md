# Research runs and Wiki retrieval

## Long-running research

Streaming turns create a durable `run_id` in `research_runs.sqlite3`. LangGraph
persists node-boundary state in `research_checkpoints.sqlite3` with synchronous
checkpoint writes. Both files live next to the knowledge-base index. On process
startup, unfinished `queued` and `running` runs become `interrupted`.

- `GET /api/sessions/{session_id}/runs` lists run state.
- `POST /api/sessions/{session_id}/runs/{run_id}/cancel` requests cancellation.
- `POST /api/sessions/{session_id}/runs/{run_id}/resume/stream` continues an
  interrupted run after verifying the selected source paths and text/image
  chunk IDs have not changed.

The first SSE `status` frame contains `run_id`. The browser's Cancel button
calls the server endpoint; closing the tab alone does not cancel a run. A
cancelled run never saves an incomplete turn. A successfully saved turn has a
unique `run_id`, so retrying persistence cannot create a duplicate answer.

Cancellation interrupts an in-flight request made by the OpenAI-compatible
HTTP client. It cannot guarantee that a provider stops computation or waives
charges after accepting a request. A node interrupted before its checkpoint
may execute again on resume and incur another model/tool call. Resume is
therefore at-least-once at the node level, not exactly-once for external
side effects. Sync `/turns` calls are unchanged and do not create resumable
runs; use `/turns/stream` for long research.

## Wiki in RAG

The Wiki tab lists, searches, reads, compiles, updates, and deletes verified
Markdown pages. Compilation requires the selected original documents and
the existing verification workflow. Wiki search supplies bounded original
chunk candidates to quick QA, research graph retrieval, and the agent's
`search_evidence` tool. It never places Wiki prose in the answer's evidence
context or treats Wiki pages as citation sources. Pages with stale or
out-of-scope source IDs are excluded. Original PDF page links remain the
source of truth.
