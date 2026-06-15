"""MCP client — bridge to external MCP servers (ADR-0016 Category A).

A connection pool keyed by server name. Opens sessions lazily on first use,
keeps them for the process lifetime, normalises every result into the same
plain dict an in-process tool returns, so the decision log records MCP calls
identically.

Transports (design decision D3):
  - stdio          : launch the server as a subprocess (local/dev MCP servers)
  - streamable_http : remote server over HTTP (the modern transport; SSE is
                      deprecated upstream and intentionally NOT implemented)

The `mcp` package is imported lazily inside `ensure_open`, so the offline/mock
path (which never opens a server) needs no MCP dependency installed.

Server configs are plain dicts (from the package's mcp_servers section or a
registry):  {"name","transport","command","args","env"} for stdio;
            {"name","transport","url","headers"} for streamable_http.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, Optional

logger = logging.getLogger("harness.mcp")


class MCPError(Exception):
    pass


class MCPServerNotOpen(MCPError):
    pass


class MCPClient:
    def __init__(self, server_configs: Optional[dict[str, dict]] = None):
        # name → config dict
        self._configs: dict[str, dict] = dict(server_configs or {})
        self._sessions: dict[str, Any] = {}
        self._stacks: dict[str, AsyncExitStack] = {}

    def register_server(self, config: dict) -> None:
        self._configs[config["name"]] = config

    def is_open(self, name: str) -> bool:
        return name in self._sessions

    async def ensure_open(self, name: str) -> None:
        if name in self._sessions:
            return
        config = self._configs.get(name)
        if config is None:
            raise MCPError(f"MCP server {name!r} is not registered. "
                           f"Known: {sorted(self._configs)}")
        await self._open(config)

    async def _open(self, config: dict) -> None:
        # Lazy imports — only needed when an MCP server is actually used.
        from mcp.client.session import ClientSession

        name = config["name"]
        transport = config.get("transport", "stdio")
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if transport == "stdio":
                from mcp.client.stdio import StdioServerParameters, stdio_client
                params = StdioServerParameters(
                    command=config["command"],
                    args=list(config.get("args", [])),
                    env=dict(config.get("env", {})) or None,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif transport in ("streamable_http", "http"):
                from mcp.client.streamable_http import streamablehttp_client
                # streamablehttp_client yields (read, write, get_session_id)
                streams = await stack.enter_async_context(
                    streamablehttp_client(config["url"], headers=config.get("headers"))
                )
                read, write = streams[0], streams[1]
            else:
                raise MCPError(
                    f"Unsupported MCP transport {transport!r} on {name!r}. "
                    "Supported: stdio, streamable_http. (SSE is deprecated upstream "
                    "and not implemented.)"
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise

        self._stacks[name] = stack
        self._sessions[name] = session
        logger.info("MCP server opened: %s (%s)", name, transport)

    async def list_tools(self, name: str) -> list[dict]:
        session = self._require(name)
        result = await session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema or {}}
            for t in result.tools
        ]

    async def call_tool(self, name: str, tool_name: str, arguments: dict) -> dict[str, Any]:
        """Dispatch and normalise to {"content":[...], "is_error":bool, "text":...}."""
        session = self._require(name)
        result = await session.call_tool(tool_name, arguments=arguments)
        content, texts = [], []
        for block in result.content:
            if hasattr(block, "text"):
                content.append({"type": "text", "text": block.text})
                texts.append(block.text)
            else:
                content.append({"type": getattr(block, "type", "unknown"), "raw": str(block)})
        normalized: dict[str, Any] = {"content": content, "is_error": bool(getattr(result, "isError", False))}
        if texts:
            normalized["text"] = "\n".join(texts)
        return normalized

    def _require(self, name: str):
        session = self._sessions.get(name)
        if session is None:
            raise MCPServerNotOpen(f"MCP server {name!r} is not open")
        return session

    async def close_all(self) -> None:
        for name in list(self._sessions):
            stack = self._stacks.pop(name, None)
            self._sessions.pop(name, None)
            if stack:
                try:
                    await stack.aclose()
                except Exception:
                    logger.exception("error closing MCP server %s", name)


__all__ = ["MCPClient", "MCPError", "MCPServerNotOpen"]
