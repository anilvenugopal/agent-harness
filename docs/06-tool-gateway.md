# Module 06 — The Tool Gateway

> Key files: `harness/tools/gateway.py`, `harness/tools/python_tools.py`,
> `harness/mcp/client.py`, `harness/mock/context.py`
>
> This module covers every stage a tool call passes through — authorization,
> mock interception, dispatch by transport, and the delegation path that
> re-enters the engine at depth+1.

---

## The gateway's role

Every tool call the model makes goes through one function:

```python
rec = await self.tools.call(
    name=tc.name,
    tool_input=tc.input,
    authorized=pkg.tools,
    call_order=i,
    mock=mock,
    delegate_ctx=self._delegate_ctx(pkg, ...),
)
```

`ToolGateway.call` is the single chokepoint for all four transports. Nothing
reaches a real tool implementation without passing through it. It enforces
authorization, applies mock interception, routes to the right backend, and
normalises every result to the same shape — so the loop and the decision log
never need to branch on transport.

The gateway's own docstring names the four steps and their order explicitly:

```
1. AUTHORIZATION ENFORCER (non-bypassable)
2. HITL GATE  <- enforced in the loop, not here; gateway exposes needs_approval
3. MOCK SEAM
4. DISPATCH
```

Every model-requested tool call travels this path before any effect occurs:

```
  model turn response
  contains ToolUseBlock(name="bind_policy", input={...})
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  ENGINE LOOP — before gateway.call                       │
  │                                                          │
  │  HITL check: needs_approval("bind_policy", pkg.hitl)?    │
  │    YES → serialise Continuation → raise HITLSuspended    │
  │    NO  ──────────────────────────────────────────────┐   │
  └──────────────────────────────────────────────────────┼───┘
                                                         │
                                                         ▼
  ┌───────────────────────────────────────────────────────────┐
  │  TOOL GATEWAY  gateway.call(name, input, authorized, ...) │
  │                                                           │
  │  Step 1 ── AUTH ──────────────────────────────────────    │
  │    name in pkg.tools? NO → return _err("unauthorized")    │
  │                      YES ──────────────────────────────   │
  │                                                      │    │
  │  Step 3 ── MOCK SEAM ─────────────────────────────── │    │
  │    mock.tool_responses[name]? YES → return _ok(canned)│   │
  │    mock.mock_all_tools?       YES → return _ok(canned)│   │
  │    tool_def.mock_response?    YES → return _ok(canned)│   │
  │                               NO ──────────────────── │   │
  │                                                      │    │
  │  Step 4 ── DISPATCH ──────────────────────────────── │    │
  │    transport == python_inprocess → _python()          │   │
  │    transport == mcp_stdio/http   → _mcp()             │   │
  │    transport == verity_builtin   → _builtin()         │   │
  │                                      │                │   │
  └──────────────────────────────────────┼────────────────┘   │
                                         ▼
                               uniform tool_record dict
                               {tool_name, output_data,
                                transport, error, ...}
                                         │
                                         ▼
                               ToolResultBlock → back to model
```

Step 2 is noted but not implemented here. The HITL check happens in the engine
loop *before* `gateway.call` is invoked, because the continuation store and
suspension logic belong to the engine, not the gateway. `needs_approval` is a
pure predicate the loop calls:

```python
def needs_approval(self, name: str, hitl_policy) -> bool:
    return bool(hitl_policy and hitl_policy.enabled and name in hitl_policy.require_approval_for)
```

---

## Step 1: authorization — the governance gate

```python
def authorize(name: str, authorized: list[ToolAuthorization]) -> Optional[ToolAuthorization]:
    return next((t for t in authorized if t.name == name), None)

# In gateway.call:
tool_def = authorize(name, authorized)
if tool_def is None:
    return _err(name, tool_input, call_order,
                f"Tool {name!r} is not authorized for this package. "
                f"Authorized: {sorted(t.name for t in authorized)}",
                transport="unauthorized")
```

If the model requests a tool not in the package's `tools` list, the gateway
returns an error `tool_record` immediately. The tool is **never dispatched** —
regardless of transport, regardless of how well-formed the request was,
regardless of whether a Python implementation exists.

The error goes back to the model as a `ToolResultBlock(is_error=True)`. The model
sees:

```
Tool 'send_email' is not authorized for this package.
Authorized: ['rate_property', 'lookup_appetite', 'property_data',
             'delegate_to_agent', 'bind_policy']
```

The model typically responds by trying a different tool or calling `submit_output`
with a referral decision.

**Why is this non-bypassable?** Authorization is checked against `pkg.tools` —
the list declared in the package YAML, loaded at engine startup. There is no
runtime flag that disables it, no special case for any tool name. A model that
has been prompt-injected into requesting `run_shell` simply gets an error back.

---

## Step 3: the mock seam

```python
if mock is not None:
    resp = mock.tool_response_for(name)        # explicit per-tool override
    if resp is not None:
        return _ok(..., resp, mock_source="context")
    if mock.mock_all_tools:
        canned = tool_def.mock_response or {"mock": True, "tool": name}
        return _ok(..., canned, mock_source="mock_all")
    # MockContext present but didn't mock this tool -> run live
else:
    # No MockContext -> fall back to per-tool package default
    if tool_def.mock_response is not None:
        return _ok(..., tool_def.mock_response, mock_source="package_default")
```

Three mock sources, checked in priority order:

| Source | When active | Declared where |
|---|---|---|
| `MockContext.tool_responses["name"]` | Explicit per-tool canned response | At call site (test, reproduce) |
| `MockContext.mock_all_tools` | Blanket — mock every tool | At call site |
| `tool_def.mock_response` | Per-tool default | Package YAML |

The `mock_source` field in the record tells the decision log which path fired.
`mock_mode: True` marks the record so audits can distinguish live vs simulated
tool calls.

**Package-level `mock_response`:** A tool in the YAML can declare:

```yaml
- name: lookup_appetite
  transport: mcp_http
  mock_response:
    in_appetite: true
    authority_tiv: 3000000
```

When no `MockContext` is present but this field is set, the canned response is
used. This is the safety valve for development: a developer without an MCP server
can still execute a full agent run.

**Mocks do not cross the delegation boundary.** When the engine delegates to a
sub-agent, it passes `mock=None`:

```python
# harness/core/engine.py — _delegate
sub = await self.run_agent(
    agent_name=child_name, context=child_context, ...,
    mock=None,   # explicitly cleared
)
```

The parent's `MockContext` does not propagate to the child. Each delegation level
is independently live or mocked.

---

## Step 4: dispatch — four transports

### `python_inprocess`

```python
async def _python(self, name, tool_input, call_order) -> dict:
    if not self.python_tools.has(name):
        return _err(..., f"No python implementation registered for {name!r}")
    try:
        result = await self.python_tools.call(name, tool_input)
        return _ok(name, tool_input, call_order, result, transport="python_inprocess")
    except Exception as e:
        return _err(..., f"Tool execution failed: {e}")
```

`PythonToolRegistry` maps names to callables:

```python
async def call(self, name: str, tool_input: dict) -> Any:
    fn = self._impls[name]
    if inspect.iscoroutinefunction(fn):
        return await fn(**tool_input)    # async function
    result = fn(**tool_input)
    if inspect.isawaitable(result):
        return await result              # sync returning a coroutine
    return result                        # plain sync
```

Tool input keys are spread as keyword arguments — `{"tiv": 1850000, "occupancy": "restaurant"}`
becomes `rate_property(tiv=1850000, occupancy="restaurant", ...)`.

Registration happens at engine setup:

```python
# scripts/demo_app.py
def build_demo_tools() -> PythonToolRegistry:
    reg = PythonToolRegistry()
    reg.register("rate_property", rate_property)
    reg.register("bind_policy",   bind_policy)
    return reg

# harness/cli.py
engine = build_default_engine(python_tools=build_demo_tools(), ...)
```

---

### `mcp_stdio` and `mcp_http`

```python
async def _mcp(self, name, tool_input, call_order, tool_def) -> dict:
    server = tool_def.mcp_server
    remote = tool_def.mcp_tool_name or name    # local alias vs. remote name
    try:
        await self.mcp_client.ensure_open(server)
        result = await self.mcp_client.call_tool(server, remote, tool_input)
        rec = _ok(name, tool_input, call_order, result, transport=tool_def.transport,
                  mcp_server=server, mcp_tool_name=remote)
        rec["error"] = bool(result.get("is_error", False))
        return rec
    except Exception as e:
        return _err(..., f"MCP dispatch failed: {e}")
```

**Local vs remote name aliasing:** The model calls tools by their local name.
`mcp_tool_name` is the optional override for the remote name:

```yaml
- name: lookup_appetite        # local name — what the model calls
  transport: mcp_http
  mcp_server: policy_service
  mcp_tool_name: lookup_appetite  # remote name — what the MCP server exposes
```

If `mcp_tool_name` is absent, the local name is used as the remote name.

**`MCPClient.ensure_open`** opens the connection lazily on first use and holds it
open for the process lifetime:

```python
async def ensure_open(self, name: str) -> None:
    if name in self._sessions:
        return   # already open
    config = self._configs.get(name)
    if config is None:
        raise MCPError(f"MCP server {name!r} not registered.")
    await self._open(config)
```

`_open` uses an `AsyncExitStack` to manage both the transport and session
lifecycles. Two transports are supported:

```python
if transport == "stdio":
    # Launch a subprocess — for local/dev MCP servers.
    params = StdioServerParameters(command=config["command"], args=..., env=...)
    read, write = await stack.enter_async_context(stdio_client(params))

elif transport in ("streamable_http", "http"):
    # Remote server over HTTP — the modern MCP transport.
    streams = await stack.enter_async_context(
        streamablehttp_client(config["url"], headers=config.get("headers")))
    read, write = streams[0], streams[1]
```

SSE transport is explicitly not implemented — deprecated upstream in the MCP spec.

**Result normalisation:** `call_tool` always returns a plain dict, regardless of
what MCP content blocks the server returned:

```python
async def call_tool(self, name, tool_name, arguments) -> dict:
    result = await session.call_tool(tool_name, arguments=arguments)
    content, texts = [], []
    for block in result.content:
        if hasattr(block, "text"):
            content.append({"type": "text", "text": block.text})
            texts.append(block.text)
        else:
            content.append({"type": getattr(block, "type", "unknown"), "raw": str(block)})
    return {
        "content": content,
        "is_error": bool(getattr(result, "isError", False)),
        "text": "\n".join(texts),
    }
```

MCP's `isError` flag maps to the gateway's `error` field via the patch after
`call_tool` returns (`rec["error"] = bool(result.get("is_error", False))`). An
in-process tool that raises goes through the `except` path; an MCP tool signals
error through its result content. Both produce `error: True` in the record.

---

### `verity_builtin` — delegation

```python
async def _builtin(self, name, tool_input, call_order, delegate_ctx) -> dict:
    if name != "delegate_to_agent":
        return _err(..., f"Unknown builtin {name!r}. Known: ['delegate_to_agent']")
    if self.delegate_fn is None or delegate_ctx is None:
        return _err(..., "Delegation not available in this context.")
    return await self.delegate_fn(tool_input, delegate_ctx)
```

`delegate_to_agent` is the only registered builtin. The gateway calls
`self.delegate_fn` — a callback wired by the engine at startup:

```python
# harness/core/engine.py — __init__
self.tools.delegate_fn = self._delegate
```

The gateway does not import the engine. The engine sets the callback on the
gateway instance. The gateway depends on a callable type, not on `ExecutionEngine`.

---

## Delegation depth — how the tree grows

Each call to `delegate_to_agent` spawns a full nested `run_agent` call. The
depth counter increments at every level. Here is the structure for the
underwriting demo:

```
underwriting_agent          depth=0
│   turn 0: delegate_to_agent → loss_history_analyst
│
└─► loss_history_analyst    depth=1
    │   turn 0: pull_loss_runs (MCP) → result
    │   turn 1: submit_output → {loss_count: 1, ...}
    │   [decision record written at depth=1]
    └─► returns ExecutionResult to parent's gateway

    (parent loop resumes at turn 1)
│   turn 1: lookup_appetite (MCP)
│   turn 2: property_data (MCP)
│   turn 3: rate_property (Python)
│   turn 4: bind_policy → HITL gate fires
│   [suspended; resumed after human decision]
│   turn 5: submit_output → {decision: "bound", ...}
└─► [decision record written at depth=0]
```

The indentation corresponds to `decision_depth` in each run's record.
`parent_decision_id` on the child's record links it to the parent's record,
forming a queryable tree. The parent's `tool_calls_made` list includes the
`delegate_to_agent` tool record whose `output_data` contains the child's
`sub_decision_log_id` — so both records are cross-linked.

---

## The delegation path in the engine

`engine._delegate` is where delegation actually runs:

```python
async def _delegate(self, tool_input: dict, ctx: DelegateContext) -> dict:
    child_name = tool_input.get("agent_name")
    child_context = tool_input.get("context")

    # Input validation.
    if not isinstance(child_name, str):
        return _builtin_err(...)
    if not isinstance(child_context, dict):
        return _builtin_err(...)

    # Depth limit.
    next_depth = ctx.decision_depth + 1
    if next_depth >= MAX_DECISION_DEPTH:
        return _builtin_err(..., f"depth {next_depth} >= MAX_DECISION_DEPTH")

    # Governance gate: is this child in the parent's delegation allowlist?
    parent = self.packages.get(ctx.parent_agent_name)
    allowed = {d.child_agent for d in (parent.delegations if parent else [])}
    if child_name not in allowed:
        return _builtin_err(..., f"Not authorized to delegate to {child_name!r}.")

    sub = await self.run_agent(
        agent_name=child_name, context=child_context,
        mock=None, depth=next_depth,
        parent_decision_id=ctx.parent_decision_id,
        workflow_run_id=ctx.workflow_run_id,
    )

    return {
        "tool_name": "delegate_to_agent", "transport": "verity_builtin",
        "error": sub.status != RunStatus.COMPLETE,
        "output_data": {
            "sub_decision_log_id": str(sub.decision_log_id) if sub.decision_log_id else None,
            "sub_status": sub.status.value,
            "output": sub.output,
            "sub_input_tokens": sub.usage.input_tokens,
            "sub_output_tokens": sub.usage.output_tokens,
        },
    }
```

**Two independent governance checks before `run_agent`:**
1. `authorize("delegate_to_agent", pkg.tools)` — must be in the tools list
2. `child_name in allowed` — must be in `pkg.delegations`

A package with `delegate_to_agent` in its tools list but an empty `delegations`
list cannot delegate to anyone. A model that tries to delegate to an unlisted
agent gets denied at the second check.

**`DelegateContext`** threads correlation IDs across the boundary:

```python
class DelegateContext:
    parent_decision_id: UUID    # links the child's log record to the parent's
    decision_depth: int         # 0 = top-level, 1 = first sub-agent, ...
    workflow_run_id: Optional[UUID]
    channel: Optional[str]
    parent_agent_name: str      # used to look up the delegation allowlist
```

These end up in the child's decision log (`parent_decision_id`, `decision_depth`),
creating a linked tree of records across the delegation chain.

From the parent loop's perspective, `delegate_to_agent` is just another tool call
that returned a dict. The dict contains the child's output, status, and token
usage. If the sub-agent failed, `error: True` is set and the model sees an error
tool result.

---

## The uniform `tool_record` shape

Every dispatch path — success or failure, any transport — returns the same dict:

```python
def _ok(name, tool_input, call_order, output, *, transport, **extra) -> dict:
    return {"tool_name": name, "call_order": call_order, "input_data": tool_input,
            "output_data": output, "transport": transport, "error": False, **extra}

def _err(name, tool_input, call_order, message, *, transport, **extra) -> dict:
    return {"tool_name": name, "call_order": call_order, "input_data": tool_input,
            "output_data": {"error": message}, "transport": transport, "error": True, **extra}
```

The loop does not branch on transport:

```python
rec = await self.tools.call(...)
assembler.add_tool_call(rec)              # recorded as-is in the decision log
results.append(ToolResultBlock(
    tool_use_id=tc.id,
    content=rec["output_data"],           # what the model sees
    is_error=rec["error"],
))
```

Additional fields (`mcp_server`, `mock_mode`, `mock_source`) are passed via
`**extra` and reach the decision log without the engine ever reading them.

---

## What the model sees vs what the log records

```
tool_record:
  tool_name:   "rate_property"
  call_order:  2
  input_data:  {tiv: 1850000, occupancy: "restaurant", ...}
  output_data: {premium: 7438.18, cope_factors: {...}}   <- MODEL SEES THIS
  transport:   "python_inprocess"                         <- LOG ONLY
  error:       false                                      <- BOTH
  mock_mode:   false                                      <- LOG ONLY
```

The model receives `output_data` and `error` only. It never knows the transport.
It cannot tell whether `rate_property` is a Python function, an MCP call, or a
canned mock response. The model reasons about tool *results*, not tool *mechanics*.

---

## Error handling guarantees

Every transport wraps execution in `try/except` and returns `_err` on any
exception. The gateway **never raises**:

```python
# python_inprocess:
try:
    result = await self.python_tools.call(name, tool_input)
    return _ok(...)
except Exception as e:
    return _err(..., f"Tool execution failed: {e}")

# mcp:
try:
    await self.mcp_client.ensure_open(server)
    result = await self.mcp_client.call_tool(...)
    return _ok(...)
except Exception as e:
    return _err(..., f"MCP dispatch failed: {e}")
```

A tool exception does not crash the run. The model gets the error message, reasons
about it, and may issue a referral decision or retry with different arguments. The
decision log captures what happened. A crash would lose the log and leave the run
in an undefined state.

---

## Checkpoint

1. A model calls `delete_database` with valid JSON. The tool is not in the
   package's `tools` list. Trace exactly what the gateway returns and what the
   model receives.

2. A `MockContext` with `mock_all_tools=True` is active. The package also
   declares a `mock_response` for `lookup_appetite`. Which value does the model
   receive, and why?

3. An MCP tool's `isError` flag is `true`. How does this surface in the
   `tool_record`? How does the model see it?

4. `mcp_tool_name` is absent from a `ToolAuthorization` with `transport: mcp_http`.
   What name is sent to the MCP server?

5. A parent agent's `MockContext` has `mock_all_tools=True`. It delegates to
   `loss_history_analyst`, which calls `pull_loss_runs`. Is `pull_loss_runs`
   mocked? Why or why not?

6. The Python function for `bind_policy` raises `ValueError("invalid premium")`.
   What does the loop receive? Does the engine crash?

7. `MAX_DECISION_DEPTH` is 3. Agent A (depth 0) delegates to B (depth 1), which
   delegates to C (depth 2). C tries to delegate to D. What happens?

When you can answer these, move to **[Module 07: Connectors — Sources and Targets](07-connectors.md)**
— how the binder resolves sources before the loop and writes targets after, and the
mechanics of the Postgres and S3 connectors.