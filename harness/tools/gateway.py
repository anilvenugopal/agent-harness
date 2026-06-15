"""Tool gateway — the single chokepoint for every tool call.

Order of operations for one model-requested tool (ADR-0016 §2):

  1. AUTHORIZATION ENFORCER (non-bypassable). Is `name` in the package's
     declared tool_authorizations? If not, return an error tool_result — the
     tool is NEVER executed, regardless of how well-formed the model's request
     was. This is the governance gate that prevents prompt-injection from
     reaching un-authorized capabilities.

  2. HITL GATE. If the package's HITL policy requires approval for this tool,
     raise HITLSuspended *before* any effect. (Wired in the loop, which owns
     the continuation store; the gateway exposes `needs_approval`.)

  3. MOCK SEAM. If a MockContext supplies a response for this tool (or
     mock_all_tools), return it without dispatching. This is the same seam
     used for offline runs and audit replay.

  4. DISPATCH by transport: python_inprocess | mcp_stdio | mcp_http |
     verity_builtin (delegate_to_agent).

Every path returns a uniform `tool_record` dict (the v1 shape) so the decision
log records all tool calls identically, and the loop wraps it into a neutral
ToolResultBlock without a transport-specific branch.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from harness.core.package import ToolAuthorization
from harness.mock.context import MockContext
from harness.tools.python_tools import PythonToolRegistry

logger = logging.getLogger("harness.tools")

# A delegate callback: (tool_input, parent_ctx) -> tool_record dict.
DelegateFn = Callable[[dict, "DelegateContext"], Awaitable[dict]]


class DelegateContext:
    """Context threaded into delegate_to_agent so the spawned sub-agent
    inherits correlation ids + depth."""
    def __init__(self, *, parent_decision_id, decision_depth, workflow_run_id,
                 channel, parent_agent_name):
        self.parent_decision_id = parent_decision_id
        self.decision_depth = decision_depth
        self.workflow_run_id = workflow_run_id
        self.channel = channel
        self.parent_agent_name = parent_agent_name


def authorize(name: str, authorized: list[ToolAuthorization]) -> Optional[ToolAuthorization]:
    """The enforcer. Returns the tool def if authorized, else None."""
    return next((t for t in authorized if t.name == name), None)


class ToolGateway:
    def __init__(
        self,
        python_tools: PythonToolRegistry,
        mcp_client=None,
        delegate_fn: Optional[DelegateFn] = None,
    ):
        self.python_tools = python_tools
        self.mcp_client = mcp_client
        self.delegate_fn = delegate_fn

    def needs_approval(self, name: str, hitl_policy) -> bool:
        return bool(hitl_policy and hitl_policy.enabled and name in hitl_policy.require_approval_for)

    async def call(
        self,
        *,
        name: str,
        tool_input: dict,
        authorized: list[ToolAuthorization],
        call_order: int,
        mock: Optional[MockContext],
        delegate_ctx: Optional[DelegateContext] = None,
    ) -> dict:
        # 1. AUTH ENFORCER
        tool_def = authorize(name, authorized)
        if tool_def is None:
            return _err(name, tool_input, call_order,
                        f"Tool {name!r} is not authorized for this package. "
                        f"Authorized: {sorted(t.name for t in authorized)}",
                        transport="unauthorized")

        # 3. MOCK SEAM  (HITL gate (step 2) is enforced in the loop before this)
        if mock is not None:
            resp = mock.tool_response_for(name)
            if resp is not None:
                return _ok(name, tool_input, call_order, resp, transport=tool_def.transport,
                           mock_mode=True, mock_source="context")
            if mock.mock_all_tools:
                canned = (tool_def.mock_response
                          or {"mock": True, "tool": name, "note": "mock_all_tools default"})
                return _ok(name, tool_input, call_order, canned, transport=tool_def.transport,
                           mock_mode=True, mock_source="mock_all")
            # else: caller is in control and didn't mock this one → run live
        else:
            # no MockContext → fall back to the package's per-tool mock default
            if tool_def.mock_response is not None:
                return _ok(name, tool_input, call_order, tool_def.mock_response,
                           transport=tool_def.transport, mock_mode=True, mock_source="package_default")

        # 4. DISPATCH
        transport = tool_def.transport
        if transport == "python_inprocess":
            return await self._python(name, tool_input, call_order)
        if transport in ("mcp_stdio", "mcp_http"):
            return await self._mcp(name, tool_input, call_order, tool_def)
        if transport == "verity_builtin":
            return await self._builtin(name, tool_input, call_order, delegate_ctx)
        return _err(name, tool_input, call_order,
                    f"Unknown transport {transport!r}", transport=transport)

    # ── transports ──
    async def _python(self, name, tool_input, call_order) -> dict:
        if not self.python_tools.has(name):
            return _err(name, tool_input, call_order,
                        f"No python implementation registered for {name!r}",
                        transport="python_inprocess")
        try:
            result = await self.python_tools.call(name, tool_input)
            return _ok(name, tool_input, call_order, result, transport="python_inprocess")
        except Exception as e:
            logger.exception("python tool failed: %s", name)
            return _err(name, tool_input, call_order, f"Tool execution failed: {e}",
                        transport="python_inprocess")

    async def _mcp(self, name, tool_input, call_order, tool_def) -> dict:
        if self.mcp_client is None:
            return _err(name, tool_input, call_order,
                        "No MCP client configured on this engine.", transport=tool_def.transport)
        server = tool_def.mcp_server
        remote = tool_def.mcp_tool_name or name
        try:
            await self.mcp_client.ensure_open(server)
            result = await self.mcp_client.call_tool(server, remote, tool_input)
            rec = _ok(name, tool_input, call_order, result, transport=tool_def.transport,
                      mcp_server=server, mcp_tool_name=remote)
            rec["error"] = bool(result.get("is_error", False))
            return rec
        except Exception as e:
            logger.exception("mcp tool failed: %s on %s", name, server)
            return _err(name, tool_input, call_order, f"MCP dispatch failed: {e}",
                        transport=tool_def.transport, mcp_server=server)

    async def _builtin(self, name, tool_input, call_order, delegate_ctx) -> dict:
        if name != "delegate_to_agent":
            return _err(name, tool_input, call_order,
                        f"Unknown builtin {name!r}. Known: ['delegate_to_agent']",
                        transport="verity_builtin")
        if self.delegate_fn is None or delegate_ctx is None:
            return _err(name, tool_input, call_order,
                        "Delegation not available in this context.", transport="verity_builtin")
        return await self.delegate_fn(tool_input, delegate_ctx)


# ── uniform tool_record builders ──
def _ok(name, tool_input, call_order, output, *, transport, **extra) -> dict:
    rec = {"tool_name": name, "call_order": call_order, "input_data": tool_input,
           "output_data": output, "transport": transport, "error": False}
    rec.update(extra)
    return rec


def _err(name, tool_input, call_order, message, *, transport, **extra) -> dict:
    rec = {"tool_name": name, "call_order": call_order, "input_data": tool_input,
           "output_data": {"error": message}, "transport": transport, "error": True}
    rec.update(extra)
    return rec


__all__ = ["ToolGateway", "DelegateContext", "authorize"]
