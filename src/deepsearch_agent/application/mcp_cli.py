"""MCP command composition; adapter modules never create the Web application."""
import argparse
import asyncio
import logging
import sys
from pathlib import Path
from dataclasses import replace
from deepsearch_agent.adapters.mcp_config import MCPSettings
from deepsearch_agent.adapters.mcp_runtime import create_mcp_server, MCPRuntimeError

async def _run_cli(args):
    from deepsearch_agent.application.bootstrap import create_app
    from deepsearch_agent.application.snapshots import snapshot_answer_service

    settings = MCPSettings.load(Path(args.env))
    settings = replace(settings, server_enabled=True)
    app_kwargs = {}
    if args.demo:
        from deepsearch_agent.agents.agent_config import AgentSettings
        from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings
        from deepsearch_agent.adapters.image_embeddings import ImageEmbeddingSettings
        from deepsearch_agent.adapters.llm_config import LLMSettings
        from deepsearch_agent.adapters.vision_config import VisionSettings
        app_kwargs = dict(llm_settings=LLMSettings(), embedding_settings=EmbeddingSettings(),
                          image_embedding_settings=ImageEmbeddingSettings(), vision_settings=VisionSettings(),
                          agent_settings=AgentSettings())
        settings = replace(settings, enabled=False, server_allow_ocr=False)
    app = create_app(index_path=Path(args.index), mcp_settings=replace(settings, server_enabled=False),
                     **app_kwargs)
    async with app.router.lifespan_context(app):
        def provider():
            with app.state.lock:
                kb = snapshot_answer_service(app.state).knowledge_base
                if settings.server_sources:
                    kb = kb.restrict_sources(set(settings.server_sources))
            return app.state.tool_session_factory(kb)

        server = create_mcp_server(provider, settings=settings)
        if args.transport == "stdio":
            await server.run_stdio_async()
        else:
            await server.run_streamable_http_async(host="127.0.0.1", port=args.port)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve scoped DeepSearch paper tools over MCP")
    parser.add_argument("--index", default="data/indexes/demo.json")
    parser.add_argument("--env", default=".env.mcp")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--demo", action="store_true", help="Offline demo: disable all cloud and external tools")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        asyncio.run(_run_cli(args))
    except (MCPRuntimeError, ValueError):
        parser.exit(1, "MCP startup failed; check optional dependencies and local configuration.\n")


if __name__ == "__main__":
    main()
