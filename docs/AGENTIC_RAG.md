# Agentic RAG

## Flow

```text
question / selected documents / authorized image attachments
  -> request-local retrieval snapshot + tool budget
  -> Supervisor (tasks, dynamically loaded skills)
  -> Researcher LangGraph subgraph
       plan one tool -> ToolNode -> observation -> plan again
       list_documents / search_evidence / read_page / ocr_page / get_evidence
       optional explicitly allowlisted external MCP tools
  -> Answerer (local evidence + original page images)
  -> Verifier (claim support, citations, omissions)
  -> Supervisor (supplement / revise / finish)
  -> persist verified answer -> SSE final output
```

Tool planning uses the current cloud gateway's structured JSON interface, not
provider-native function calling. LangGraph executes real tools and feeds their
observations back to the planner. Authorized uploaded images and newly retrieved
local visual pages are also sent to the Researcher through the existing visual
adapter; image counts and the visual context budget still apply. `search_evidence` calls the existing hybrid
retrieval and reranker, not another complete answer agent. Ordinary QA and the
previous query-planning Researcher remain available only in `deepsearch_experiments.legacy`.
The optional single-query search policy is used by that previous Researcher;
the agentic path instead delegates each tool/query decision to its planner.

The agentic path now uses explicit bounded task/result envelopes. The Researcher
does not automatically inherit the parent conversation or its evidence dump;
the Supervisor receives concise candidate findings and evidence references rather
than tool observations. Original evidence still reaches the Answerer and Verifier.
See [Subagent context boundaries](SUBAGENT_CONTEXT.md) and [ADR-0009](adr/0009-subagent-context-boundary.md).

## Configuration

Live application startup loads `.env.agent`, falling back to the defaults in
`.env.agent.example`. `AGENTIC_RAG_ENABLED=true` is the live production path. Explicit `false` raises a
migration error with the offline baseline entrypoint; it no longer selects the earlier
workflow. Injected live settings also default to the production child agent. Demo
mode stays offline. See [current architecture](ARCHITECTURE.md).

Budgets are shared across all Researcher rounds of one answer: 18 tool calls,
eight planner steps per round, two OCR attempts, 30 gathered evidence chunks,
and eight distinct page images by default. The image cap also respects the
configured visual model cap. The 300-second research deadline stops admission
of additional tool calls; it cannot cancel a cloud request already running.
Each provider still enforces its own request timeout. The existing outer graph
also bounds research/review/dispatch rounds. OCR cache hits count toward the
OCR attempt cap conservatively.

`ocr_page` requires `.env.vision`. It sends the actual authorized page image
for full-page transcription and chart/table/formula analysis. Its strategy cache
is separate from ingestion's supplementary OCR cache. OCR-derived chunks are
request-local, retain page/source/content/chunk IDs, and do not mutate the text
or image index. Cloud OCR, reranking, planning, answering, and verification may
incur fees. No paid dataset evaluation is automatically launched by this feature.

MCP is an optional transport dependency; see [MCP.md](MCP.md).
The shared business service is in `tool_service.py`; LangGraph wrappers live in
`tool_adapters.py`, the planner in `agentic_research.py`, and MCP transports in
`mcp_runtime.py`. Model gateways and retrieval implementations remain separate.

## Trust Boundary

- Tool services accept exact authorized source IDs and validated page numbers,
  never arbitrary filesystem paths or model-generated shell commands.
- The request's index snapshot and attachment scope are fixed before model work.
  A tool source filter can narrow but cannot widen that scope.
- Only the local tool service can register authoritative evidence. External MCP
  observations, skill text, history, and memory cannot become local citations.
- External MCP image blocks are omitted from the planner's observations, with an
  explicit marker; they are not serialized as base64 text or treated as trusted
  local page images. The MCP adapter itself retains bounded media for other uses.
- Model observations are bounded excerpts; source references retain the original
  evidence IDs. Local source evidence is checked by the existing Verifier.
- The index lock is released during model and tool execution. Turns within the
  same conversation remain serialized; different conversations can proceed.
- Hosted tracing remains explicitly disabled; the optional existing local trace
  recorder adds tool names, call counts, chunk IDs, and stop reasons.

## Verification Boundary

Offline fixtures exercise tool selection and feedback, source isolation, original
IDs, OCR cache separation, budgets, errors, and Verifier integration. Local MCP
protocol tests use local fixtures, not paid models. Historical ChartQA/PDFVQA
results predate this agent loop and are not evidence of its end-to-end accuracy.
An Agentic RAG cloud acceptance run and a matched-budget accuracy comparison
remain separate experiments.
