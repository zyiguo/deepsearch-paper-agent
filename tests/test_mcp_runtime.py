import asyncio
import base64
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from deepsearch_agent.mcp_config import MCPServerConfig, MCPSettings
from deepsearch_agent.mcp_runtime import MCPRuntimeError, create_mcp_server, external_tools


def test_mcp_defaults_do_not_enable_connections():
    settings = MCPSettings()
    assert not settings.enabled
    assert not settings.server_enabled

    async def run():
        async with external_tools(settings) as tools:
            assert tools == []

    asyncio.run(run())


@pytest.mark.parametrize("url", [
    "http://example.com/mcp", "https://example.com/mcp",
    "http://localhost/mcp?token=secret", "http://user:secret@localhost/mcp",
    "file:///secret", "http://localhost/mcp#secret",
])
def test_external_http_requires_explicit_safe_target(url):
    with pytest.raises(ValueError):
        MCPServerConfig(name="reader", transport="http", url=url, allowed_tools=("read",))


def test_remote_http_needs_https_and_opt_in():
    config = MCPServerConfig(name="reader", transport="http", url="https://example.com/mcp",
                             allowed_tools=("read",), allow_remote=True)
    assert config.allow_remote
    with pytest.raises(ValueError):
        MCPServerConfig(name="reader", transport="http", url="http://example.com/mcp",
                        allowed_tools=("read",), allow_remote=True)


def test_every_external_server_requires_allowlist():
    with pytest.raises(ValueError):
        MCPServerConfig(name="reader", transport="stdio", command="python")


def test_config_load_is_local_and_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", "environment-secret")
    before = dict(os.environ)
    config_file = tmp_path / "servers.json"
    config_file.write_text(json.dumps([{
        "name": "reader", "transport": "http", "url": "http://127.0.0.1:8001/mcp/",
        "allowed_tools": ["read"], "token_env": "TEST_MCP_TOKEN",
    }]), encoding="utf-8")
    env_file = tmp_path / ".env.mcp"
    env_file.write_text("MCP_ENABLED=true\nMCP_SERVERS_FILE=servers.json\nTEST_MCP_TOKEN=file-secret\n", encoding="utf-8")
    settings = MCPSettings.load(env_file)
    assert settings.servers[0].bearer_token == "file-secret"
    assert "file-secret" not in repr(settings)
    assert "file-secret" not in repr(settings.servers[0])
    assert dict(os.environ) == before


class FakeClient:
    def __init__(self, tools, result=None, error=None):
        self.tools = tools
        self.result = result or SimpleNamespace(content=[SimpleNamespace(type="text", text="observation")],
                                               structured_content={"chunk_id": "forged"}, is_error=False)
        self.error = error
        self.active = False
        self.calls = []

    async def __aenter__(self):
        self.active = True
        return self

    async def __aexit__(self, *args):
        self.active = False

    async def list_tools(self, cursor=None):
        assert self.active
        return SimpleNamespace(tools=self.tools, next_cursor=None)

    async def call_tool(self, name, arguments):
        assert self.active
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result


def spec(name="read", schema=None, description="Read a document"):
    return SimpleNamespace(name=name, description=description, input_schema=schema or {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    })


def settings_for(*names, **kwargs):
    return MCPSettings(enabled=True, servers=(MCPServerConfig(
        name="reader", transport="http", url="http://127.0.0.1:8001/mcp/",
        allowed_tools=names or ("read",),
    ),), **kwargs)


def invoke(tool, arguments):
    return tool.ainvoke({"name": tool.name, "args": arguments, "id": "call-1", "type": "tool_call"})


def test_external_tools_are_allowlisted_and_keep_client_alive():
    client = FakeClient([spec(), spec("destroy")])

    async def run():
        async with external_tools(settings_for(), client_factory=lambda _: client) as tools:
            assert [tool.name for tool in tools] == ["mcp_reader__read"]
            assert client.active
            message = await invoke(tools[0], {"query": "scans"})
            assert "observation" in str(message.content)
            assert message.artifact["trust"] == "external"
            assert "chunks" not in message.artifact
        assert not client.active

    asyncio.run(run())
    assert client.calls == [("read", {"query": "scans"})]


def test_external_arguments_are_validated_before_network_call():
    client = FakeClient([spec()])

    async def run():
        async with external_tools(settings_for(), client_factory=lambda _: client) as tools:
            message = await invoke(tools[0], {"query": 42})
            assert message.status == "error"
            assert "invalid arguments" in str(message.content)
    asyncio.run(run())
    assert client.calls == []


def test_external_results_are_bounded_and_never_emit_remote_errors():
    secret = "token=super-private"
    client = FakeClient([spec()], error=RuntimeError(secret))

    async def run():
        async with external_tools(settings_for(), client_factory=lambda _: client) as tools:
            message = await invoke(tools[0], {"query": "test"})
            assert message.status == "error"
            assert secret not in str(message)
    asyncio.run(run())


def test_remote_error_payload_is_not_forwarded_to_model():
    client = FakeClient([spec()], result=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="token=secret")],
        structured_content={"secret": "credential"}, is_error=True))

    async def run():
        async with external_tools(settings_for(), client_factory=lambda _: client) as tools:
            message = await invoke(tools[0], {"query": "test"})
            assert message.status == "error"
            assert "secret" not in str(message)
    asyncio.run(run())


def test_oversized_result_and_image_are_discarded():
    client = FakeClient([spec()], result=SimpleNamespace(content=[
        SimpleNamespace(type="text", text="x" * 9000),
        SimpleNamespace(type="image", data=base64.b64encode(b"x" * 10000).decode(), mime_type="image/png"),
    ], structured_content={"large": "x" * 9000}, is_error=False))

    async def run():
        async with external_tools(settings_for(max_result_chars=2048, max_image_bytes=1024),
                                  client_factory=lambda _: client) as tools:
            message = await invoke(tools[0], {"query": "test"})
            assert len(str(message.content)) < 2600
            assert message.artifact["truncated"]
            assert message.artifact["data"] is None
            assert "image" not in str(message.content)
    asyncio.run(run())


@pytest.mark.parametrize("reference", ["$ref", "$dynamicRef", "$recursiveRef", "$id"])
def test_external_schema_must_not_load_remote_references(reference):
    client = FakeClient([spec(schema={"type": "object", reference: "https://malicious.invalid/schema"})])

    async def run():
        with pytest.raises(MCPRuntimeError, match="schema"):
            async with external_tools(settings_for(), client_factory=lambda _: client):
                pass
        assert not client.active
    asyncio.run(run())


def test_external_valid_image_block_is_preserved_without_becoming_local_evidence():
    encoded = base64.b64encode(b"small image observation").decode()
    client = FakeClient([spec()], result=SimpleNamespace(content=[
        SimpleNamespace(type="image", data=encoded, mime_type="image/png"),
    ], structured_content=None, is_error=False))

    async def run():
        async with external_tools(settings_for(), client_factory=lambda _: client) as tools:
            message = await invoke(tools[0], {"query": "image"})
            assert message.content[-1] == {"type": "image", "base64": encoded, "mime_type": "image/png"}
            assert message.artifact["trust"] == "external"
            assert not message.artifact["truncated"]
    asyncio.run(run())


def test_external_timeout_is_sanitized():
    class SlowClient(FakeClient):
        async def call_tool(self, name, arguments):
            await asyncio.sleep(1)

    client = SlowClient([spec()])

    async def run():
        async with external_tools(settings_for(timeout_seconds=0.01), client_factory=lambda _: client) as tools:
            message = await invoke(tools[0], {"query": "image"})
            assert message.status == "error"
            assert "timed out" in str(message.content)
    asyncio.run(run())


def test_config_rejects_nonboolean_enable_flags():
    with pytest.raises(ValueError):
        MCPSettings(enabled="false")


def test_stdio_argv_secrets_are_redacted():
    config = MCPServerConfig(name="reader", transport="stdio", command="python",
                             allowed_tools=("read",), args=("--token", "argv-secret"))
    assert "argv-secret" not in repr(config)


def test_config_rejects_tool_names_too_long_for_graph_actions():
    with pytest.raises(ValueError, match="96"):
        MCPServerConfig(name="reader", transport="stdio", command="python",
                        allowed_tools=("x" * 90,))


def test_server_protocol_delegates_to_new_session_for_each_call():
    mcp = pytest.importorskip("mcp")
    if not hasattr(mcp, "Client"):
        pytest.skip("MCP SDK v2 is required")
    sessions = []
    instances = []

    class Session:
        def __init__(self):
            instances.append(self)

        def invoke(self, name, arguments):
            sessions.append((id(self), name, arguments))
            return SimpleNamespace(content="local evidence", chunks=[], data={"source": "pdf://known"})

    server = create_mcp_server(Session, settings=MCPSettings(server_enabled=True, server_allow_ocr=True))

    async def run():
        async with mcp.Client(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "list_documents", "search_evidence", "read_page", "ocr_page", "get_evidence"}
            result = await client.call_tool("search_evidence", {"query": "figures"})
            assert not result.is_error, str(result)
            assert result.structured_content["data"]["source"] == "pdf://known"
            result = await client.call_tool("read_page", {"source": "pdf://known", "page": 2})
            assert not result.is_error
    asyncio.run(run())
    assert [item[1] for item in sessions] == ["search_evidence", "read_page"]
    assert sessions[0][0] != sessions[1][0]


def test_server_denies_ocr_by_default_and_sanitizes_failures():
    mcp = pytest.importorskip("mcp")
    if not hasattr(mcp, "Client"):
        pytest.skip("MCP SDK v2 is required")

    class Session:
        def invoke(self, name, arguments):
            raise RuntimeError("credential=private")

    server = create_mcp_server(Session)

    async def run():
        async with mcp.Client(server) as client:
            ocr = await client.call_tool("ocr_page", {"source": "pdf://known", "page": 1})
            assert ocr.is_error
            assert "credential" not in str(ocr)
            read = await client.call_tool("read_page", {"source": "pdf://known", "page": 1})
            assert read.is_error
            assert "private" not in str(read)
    asyncio.run(run())


def test_server_generated_evidence_can_be_resolved_without_widening_scope():
    mcp = pytest.importorskip("mcp")
    from deepsearch_agent.chunking import TextChunk
    chunk = TextChunk("recognized figure", "pdf://known", 1, 0)
    sources = {"pdf://known"}

    class Session:
        def __init__(self):
            self.sources = frozenset(sources)
            self.registry = {}

        def invoke(self, name, arguments):
            if name == "read_page":
                return SimpleNamespace(content="recognized figure", chunks=[chunk], data={"chunk_id": chunk.chunk_id})
            resolved = self.registry[arguments["chunk_id"]]
            return SimpleNamespace(content=resolved.text, chunks=[resolved], data={"chunk_id": resolved.chunk_id})

    server = create_mcp_server(Session)

    async def run():
        async with mcp.Client(server) as client:
            page = await client.call_tool("read_page", {"source": "pdf://known", "page": 1})
            assert not page.is_error
            evidence = await client.call_tool("get_evidence", {"chunk_id": chunk.chunk_id})
            assert not evidence.is_error
            assert evidence.structured_content["content"] == "recognized figure"
            sources.clear()
            forbidden = await client.call_tool("get_evidence", {"chunk_id": chunk.chunk_id})
            assert forbidden.is_error
    asyncio.run(run())


def test_real_stdio_transport(tmp_path):
    mcp = pytest.importorskip("mcp")
    program = tmp_path / "stdio_server.py"
    program.write_text('''from types import SimpleNamespace
from deepsearch_agent.mcp_runtime import create_mcp_server
class Session:
    def invoke(self, name, arguments):
        return SimpleNamespace(content="stdio evidence", data={"ok": True}, chunks=[])
create_mcp_server(Session).run(transport="stdio")
''', encoding="utf-8")

    async def run():
        target = mcp.StdioServerParameters(command=sys.executable, args=[str(program)])
        async with mcp.Client(target) as client:
            catalog = await client.list_tools()
            assert len(catalog.tools) == 5
            result = await client.call_tool("list_documents", {})
            assert result.structured_content["content"] == "stdio evidence"
    asyncio.run(run())


def test_real_module_cli_supports_nonempty_default_source_scope_in_demo_mode(tmp_path):
    mcp = pytest.importorskip("mcp")
    from deepsearch_agent.demo import build_demo_knowledge_base
    index = tmp_path / "demo.json"
    build_demo_knowledge_base().save(index)
    env = tmp_path / ".env.mcp"
    env.write_text("MCP_ENABLED=false\nMCP_SERVER_SOURCES=[]\n", encoding="utf-8")

    async def run():
        target = mcp.StdioServerParameters(command=sys.executable, args=[
            "-m", "deepsearch_agent.mcp_runtime", "--demo", "--index", str(index), "--env", str(env),
        ])
        async with mcp.Client(target) as client:
            catalog = await client.call_tool("list_documents", {})
            assert not catalog.is_error
            assert catalog.structured_content["data"]
            found = await client.call_tool("search_evidence", {"query": "research"})
            assert not found.is_error
    asyncio.run(run())


def test_real_http_client_preserves_bearer_header_and_lifetime():
    mcp = pytest.importorskip("mcp")
    import uvicorn

    server = mcp.server.MCPServer("External test")

    @server.tool()
    def read(query: str) -> dict[str, str]:
        return {"value": query}

    app = server.streamable_http_app()
    observed = []

    class AuthorizedApp:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                authorization = dict(scope["headers"]).get(b"authorization")
                observed.append(authorization)
                if authorization != b"Bearer integration-secret":
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"forbidden"})
                    return
            await app(scope, receive, send)

    async def run():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        config = uvicorn.Config(AuthorizedApp(), host="127.0.0.1", port=port, log_level="critical")
        host = uvicorn.Server(config)
        task = asyncio.create_task(host.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if host.started:
                    break
                await asyncio.sleep(0.02)
            assert host.started
            settings = MCPSettings(enabled=True, servers=(MCPServerConfig(
                name="reader", transport="http", url=f"http://127.0.0.1:{port}/mcp",
                bearer_token="integration-secret", allowed_tools=("read",),
            ),))
            async with external_tools(settings) as tools:
                message = await invoke(tools[0], {"query": "diagram"})
                assert message.artifact["data"] == {"value": "diagram"}
                assert message.artifact["trust"] == "external"
        finally:
            host.should_exit = True
            await asyncio.wait_for(task, 5)
            listener.close()
    asyncio.run(run())
    assert observed and set(observed) == {b"Bearer integration-secret"}
