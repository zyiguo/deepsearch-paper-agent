# Agentic RAG and MCP Implementation

Status: implemented and locally verified on 2026-10-05.

## Boundaries

Keep the existing Supervisor, Answerer, Verifier, citations, skills, memory,
multimodal retrieval, and ordinary QA behavior. Add model-chosen tools to the
Researcher, using structured JSON planning for compatibility with the existing
cloud gateway. Tool execution uses LangGraph ToolNode. No nested answer agent.

## Modules

- `tool_service.py`: request-scoped corpus, five business tools, authoritative
  evidence registry, OCR cache, shared local/external call budget. No transport.
- `agent_config.py`: validated opt-in/out configuration and budgets.
- `tool_adapters.py`: LangGraph StructuredTool wrappers, separate from business logic.
- `agentic_research.py`: bounded planner / ToolNode / observation loop.
- `mcp_config.py`, `mcp_runtime.py`: optional SDK dependency, local server,
  allowlisted external clients, protocol and authentication boundaries.
- `qa.py`, `research_graph.py`: additive Researcher integration, old mode kept.
- `api.py`, `conversation_api.py`: scoped immutable index snapshot under lock;
  run cloud work outside the shared index lock. Optional MCP HTTP lifespan.

## Trust and Cost

Only indexed sources and this-turn authorized attachments can produce local
evidence. Tools accept source IDs, never arbitrary paths. External observations
are untrusted and cannot produce local citations. Source scope can only narrow.
Forced page OCR uses a distinct model/strategy cache key from ingestion.
Budgets span all Researcher rounds in one answer. MCP defaults disabled and binds
loopback. Configured stdio commands are trusted operator configuration only.
No new paid dataset evaluation is part of this implementation.

## Test Sequence

1. RED/GREEN shared service scope, IDs, OCR cache, schemas, and budgets.
2. RED/GREEN LangGraph tool decisions, feedback, errors, async tools, provenance.
3. RED/GREEN MCP config, real SDK protocol, allowlists, resource lifetime.
4. Integration tests for verification/citation retention and index lock release.
5. Full backend regression suite, independent security/contract review, local
   startup and health/protocol smoke tests. Document what was and was not run.

Work in the existing dirty checkout; preserve all earlier multimodal and
evaluation changes. Do not create commits or change historical benchmark data.

## Verification Result

- Final backend suite: 431 passed, 1 skipped (48.36 seconds).
- Real SDK in-process, stdio, loopback HTTP/bearer, project CLI, and mounted
  FastAPI lifecycle verified with offline fixtures.
- Independent review issues fixed: CLI empty source scope, external tool name
  length, original-image planner access, and external image/base64 observations.
- Dependency check reports no broken requirements.
- Local frontend and backend started; frontend HTTP and proxy health checks pass.
  Live configuration enables Agentic RAG, vision, and image retrieval; MCP remains
  disabled. The loaded index has no persistence error or write block.
- No paid cloud model call or new dataset benchmark was run. Accuracy and
  matched-budget comparisons are not claimed by these tests.
