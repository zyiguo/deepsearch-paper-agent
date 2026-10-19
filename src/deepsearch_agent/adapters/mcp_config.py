import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    allowed_tools: tuple[str, ...] = ()
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = field(default=(), repr=False)
    cwd: str | None = None
    allow_remote: bool = False
    bearer_token: str = field(default="", repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not _NAME.fullmatch(self.name):
            raise ValueError("MCP server name must be a short ASCII identifier")
        if self.transport not in {"http", "stdio"}:
            raise ValueError("MCP transport must be http or stdio")
        if (not self.allowed_tools or len(self.allowed_tools) > 24
                or len(set(self.allowed_tools)) != len(self.allowed_tools)
                or any(not isinstance(name, str) or not _TOOL.fullmatch(name)
                       for name in self.allowed_tools)):
            raise ValueError("Every MCP server requires an explicit tool allowlist")
        if not isinstance(self.allow_remote, bool):
            raise ValueError("MCP allow_remote must be boolean")
        if any(len(f"mcp_{self.name}__{name}") > 96 for name in self.allowed_tools):
            raise ValueError("MCP namespaced tool names must fit 96 characters; shorten server or tool names")
        if self.transport == "http":
            url = urlsplit(self.url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment
                    or (not _loopback(url.hostname) and not self.allow_remote)
                    or (not _loopback(url.hostname) and url.scheme != "https")
                    or self.command or self.args or self.cwd or self.env):
                raise ValueError("MCP HTTP target must be loopback or explicitly allowed HTTPS")
            try:
                _ = url.port
            except ValueError:
                raise ValueError("MCP HTTP port is invalid") from None
        elif (not self.command.strip() or self.url or self.bearer_token
              or len(self.args) > 64 or any(not isinstance(arg, str) or len(arg) > 4096 for arg in self.args)
              or (self.cwd is not None and not Path(self.cwd).is_absolute())
              or any(not isinstance(key, str) or not isinstance(value, str) for key, value in self.env.items())):
            raise ValueError("MCP stdio requires trusted command, argv and absolute cwd")


@dataclass(frozen=True)
class MCPSettings:
    enabled: bool = False
    server_enabled: bool = False
    server_allow_ocr: bool = False
    server_sources: tuple[str, ...] = ()
    servers: tuple[MCPServerConfig, ...] = ()
    timeout_seconds: float = 30.0
    max_tools: int = 12
    max_metadata_chars: int = 32768
    max_result_chars: int = 16000
    max_image_bytes: int = 1_000_000

    def __post_init__(self):
        if any(not isinstance(flag, bool) for flag in (self.enabled, self.server_enabled, self.server_allow_ocr)):
            raise ValueError("MCP enable flags must be boolean")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("MCP timeout must be within 0 and 120 seconds")
        if (not 1 <= self.max_tools <= 24 or not 2048 <= self.max_metadata_chars <= 100000
                or not 1024 <= self.max_result_chars <= 64000
                or not 1024 <= self.max_image_bytes <= 3_000_000):
            raise ValueError("MCP tool, metadata, result or image budget is invalid")
        if len(self.servers) > 8 or len({server.name for server in self.servers}) != len(self.servers):
            raise ValueError("MCP servers must have unique names and at most eight entries")
        if (len(self.server_sources) > 1000
                or any(not isinstance(source, str) or not source.strip() for source in self.server_sources)):
            raise ValueError("MCP server source scope is invalid")

    @classmethod
    def load(cls, env_file: Path, *, environ: Mapping[str, str] | None = None):
        values = dict(os.environ if environ is None else environ)
        if env_file.exists():
            values.update({key: value for key, value in dotenv_values(
                env_file, encoding="utf-8-sig", interpolate=False).items() if value is not None})

        def value(name, default=""):
            return values.get(name, default).strip()

        def boolean(name):
            raw = value(name, "false").lower()
            if raw not in {"true", "false", "1", "0"}:
                raise ValueError(f"{name} must be true or false")
            return raw in {"true", "1"}

        try:
            timeout = float(value("MCP_TIMEOUT_SECONDS", "30"))
            max_tools = int(value("MCP_MAX_TOOLS", "12"))
            max_metadata = int(value("MCP_MAX_METADATA_CHARS", "32768"))
            max_result = int(value("MCP_MAX_RESULT_CHARS", "16000"))
            max_image = int(value("MCP_MAX_IMAGE_BYTES", "1000000"))
            sources = json.loads(value("MCP_SERVER_SOURCES", "[]"))
        except (ValueError, TypeError):
            raise ValueError("MCP numeric budgets or source scope are invalid") from None
        if not isinstance(sources, list):
            raise ValueError("MCP_SERVER_SOURCES must be a JSON array")
        servers = []
        servers_file = value("MCP_SERVERS_FILE")
        if servers_file:
            path = Path(servers_file)
            if not path.is_absolute():
                path = env_file.parent / path
            try:
                if path.stat().st_size > 100000:
                    raise ValueError("MCP server configuration is too large")
                entries = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                raise ValueError("MCP server configuration must be a readable JSON array") from None
            if not isinstance(entries, list) or len(entries) > 8:
                raise ValueError("MCP server configuration must contain at most eight servers")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("MCP server configuration entries must be objects")
                allowed = {"name", "transport", "allowed_tools", "url", "command", "args", "cwd",
                           "allow_remote", "token_env", "env"}
                if set(entry) - allowed:
                    raise ValueError("MCP server configuration contains unknown fields")
                try:
                    item = dict(entry)
                    token_env = item.pop("token_env", "")
                    if not isinstance(token_env, str):
                        raise ValueError("MCP token_env must be a string")
                    item["bearer_token"] = value(token_env) if token_env else ""
                    if token_env and not item["bearer_token"]:
                        raise ValueError("Configured MCP bearer token is missing")
                    for key in ("allowed_tools", "args"):
                        if key in item:
                            if not isinstance(item[key], list):
                                raise ValueError("MCP allowlist and argv must be JSON arrays")
                            item[key] = tuple(item[key])
                    servers.append(MCPServerConfig(**item))
                except (TypeError, AttributeError):
                    raise ValueError("MCP server configuration is invalid") from None
        return cls(enabled=boolean("MCP_ENABLED"), server_enabled=boolean("MCP_SERVER_ENABLED"),
                   server_allow_ocr=boolean("MCP_SERVER_ALLOW_OCR"), server_sources=tuple(sources),
                   servers=tuple(servers), timeout_seconds=timeout, max_tools=max_tools,
                   max_metadata_chars=max_metadata, max_result_chars=max_result,
                   max_image_bytes=max_image)
