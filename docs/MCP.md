# MCP Integration

The MCP layer is optional. It uses the official Python SDK v2, not the archived
`langchain-mcp-adapters` package or a separate FastMCP dependency. Local business
logic remains in `PaperToolSession`; the protocol adapter does not run a nested QA
agent or implement another retrieval pipeline.

Install the project's optional dependencies:

```powershell
.venv\Scripts\python.exe -m pip install -c constraints-security.txt -e '.[mcp]'
```

Configuration comes from `.env.mcp` and the environment. The local file takes
precedence without modifying process environment variables. Both directions are
disabled by default. `.env.mcp.example` lists the available settings.

## Expose Local Paper Tools

Set `MCP_SERVER_ENABLED=true` to mount the server under the backend's `/mcp/`
endpoint. The host app enters the MCP session-manager lifespan; mounting the ASGI
child alone would not start its background resources.

The five exported tools are:

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `list_documents` | none | List the server's authorized document catalog |
| `search_evidence` | `query`, `top_k=5`, optional `sources` | Retrieve evidence, with only narrower source filters |
| `read_page` | `source`, `page` | Read an existing document page |
| `ocr_page` | `source`, `page` | Recognize an existing page image on demand |
| `get_evidence` | `chunk_id` | Resolve an authorized stable evidence ID |

The `source` arguments are catalog identifiers, not filesystem paths. All tools
use a fresh scoped service session. Dynamic page/OCR chunks can be resolved by a
later `get_evidence` call through a server-local cache capped at 100 chunks and
500,000 text characters. A fresh session's source scope is checked before cached
chunks are exposed, so the cache cannot widen a request's scope.

`MCP_SERVER_SOURCES` is a JSON array of exact source URIs. An empty array allows
the local provider's already authorized corpus; configure a nonempty allowlist
when sharing only selected documents. `MCP_SERVER_ALLOW_OCR=false` prevents
external callers from triggering OCR. Turning it on may incur cloud model
charges. Retrieval can also incur configured embedding/reranking charges.

Standalone stdio and loopback HTTP use the same app-managed resources:

```powershell
.venv\Scripts\python.exe -m deepsearch_agent.mcp_runtime --index data/indexes/demo.json --transport stdio
.venv\Scripts\python.exe -m deepsearch_agent.mcp_runtime --index data/indexes/demo.json --transport http --port 8001
.venv\Scripts\python.exe -m deepsearch_agent.mcp_runtime --demo --index data/indexes/demo.json --transport stdio
```

The HTTP sidecar binds only `127.0.0.1`; its endpoint is `/mcp`. Stdio reserves
stdout for the MCP protocol and sends application logs to stderr. The module CLI
and app mount are alternatives, not two servers that must both be started.
`--demo` overrides cloud, OCR and outbound MCP settings with offline providers for
protocol checks; otherwise the normal project model configuration is retained.

This is a local-only server without an application authentication layer. Do not
expose it publicly, broaden its Host/Origin allowlists, or reverse-proxy it to an
untrusted network. Public deployment needs authentication, authorization,
rate-limiting and deployment-level request budgets first.

## Connect Approved External Tools

Set `MCP_ENABLED=true` and `MCP_SERVERS_FILE` to a trusted JSON file. Each entry
requires an explicit tool-name allowlist. For example:

```json
[
  {
    "name": "paper_catalog",
    "transport": "http",
    "url": "http://127.0.0.1:8001/mcp",
    "allowed_tools": ["list_documents", "search_evidence"],
    "token_env": "PAPER_MCP_TOKEN"
  }
]
```

`token_env` is optional. When provided it must name a nonempty secret from the
local environment configuration; the adapter sends it as a bearer header, never
as a query parameter. Remote HTTP targets require `allow_remote: true` and HTTPS.
URLs with credentials, query parameters or fragments are rejected. Proxy
environment variables are not inherited by this outbound MCP HTTP client.

A trusted stdio entry uses `command`, an array of `args`, optional absolute `cwd`
and optional explicit `env`. Commands and arguments are configuration, never
model-selected values. The SDK provides a minimal child-process environment;
pass any necessary values explicitly. Prefer an absolute executable path.

```json
[
  {
    "name": "local_catalog",
    "transport": "stdio",
    "command": "C:\\path\\to\\python.exe",
    "args": ["C:\\path\\to\\trusted_server.py"],
    "cwd": "C:\\path\\to\\project",
    "allowed_tools": ["search_papers"]
  }
]
```

`external_tools(settings)` is an async context manager. Its clients stay open
for the entire graph run; calling wrappers after leaving that context is not
supported. Discovered names become `mcp_<server-name>__<tool-name>` to avoid
collisions with local tools. JSON Schema validates arguments before an outbound
call. A namespaced name must fit the graph's 96-character action field; shorten
the configured server or allowlisted tool name if configuration validation fails.
Nonlocal schema references are rejected, catalog pagination and metadata
are bounded, and no resource URLs are automatically fetched.

Text, images and structured results are bounded. Structured data is preserved in
an artifact marked `trust="external"`; it never supplies trusted local chunks or
creates local citations, even when it contains a forged `chunk_id`. External
instructions are observations, not system instructions. Unsupported media and
oversized values are omitted rather than fetched or executed. Transport and
server error details are not copied into model prompts or application errors.

## Budgets And Verification

The agent's own per-run call budget still applies to external tools. The MCP
adapter additionally bounds each tool's timeout, metadata, text and image size.
The exported server uses fresh per-call budgets and at most four concurrent
workers. A timeout returns an error but cannot forcibly stop an already running
synchronous model request; that work retains its worker slot until it finishes.
The model client's own timeout remains the underlying network bound. These are
not cumulative per-user rate limits or a public billing-control system.

Offline tests cover default-disabled configuration, narrowing scope, allowlists,
argument validation, secret-safe errors, result/image budgets, client lifetime,
and genuine SDK in-process, stdio and loopback HTTP calls including bearer
headers. They do not establish answer quality on cloud models, production OAuth
interoperability, or compatibility with every third-party MCP server. No new paid
cloud benchmark was started for this integration.

Official references:

- [Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Client lifecycle and results](https://py.sdk.modelcontextprotocol.io/client/)
- [Client transports](https://py.sdk.modelcontextprotocol.io/client/transports/)
- [Mounting an MCP ASGI app](https://py.sdk.modelcontextprotocol.io/run/asgi/)
