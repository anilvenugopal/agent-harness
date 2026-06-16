# Module 04 — The Execution Engine

> Key file: `harness/core/engine.py`
>
> This module walks through every stage of the engine — both paths (task and agent),
> the full agentic loop, quota enforcement, HITL, delegation, target writes, and
> resume. Read this alongside the source file; the code excerpts below are lightly
> trimmed but structurally exact.

---

## Two paths in one engine

The engine exposes three public methods:

```python
await engine.run_task(task_name="classify_document", input_data={...})
await engine.run_agent(agent_name="underwriting_agent", context={...})
await engine.resume(suspension_id=UUID("..."))
```

`run_task` and `run_agent` each resolve sources, build the opening user message,
and then diverge:

```
run_task()
  └─ resolve sources
  └─ build first user message (text + optional document/image blocks)
  └─ one model call, force structured_output tool
  └─ extract output from tool call
  └─ write targets
  └─ write decision log → return ExecutionResult

run_agent()
  └─ resolve sources
  └─ build first user message
  └─ _agent_loop()          ← the shared multi-turn loop
       └─ quota check
       └─ model call
       └─ stop_reason == TOOL_USE?
          ├─ no → done
          └─ yes → submit_output? / HITL? / dispatch tools
       └─ turn += 1, loop
  └─ write targets
  └─ write decision log → return ExecutionResult

resume()
  └─ load Continuation from store
  └─ rebuild messages + assembler from checkpoint
  └─ apply human decision to the pending tool call
  └─ _agent_loop()          ← same loop, same code, starting at turn+1
  └─ write targets
  └─ write decision log → return ExecutionResult
```

`_agent_loop` is the core. Both a fresh `run_agent` and a `resume` enter it —
which is what makes the engine a resumable state machine without duplicating the
loop code.

---

## The task path

```python
# harness/core/engine.py — run_task (condensed)

async def run_task(self, *, task_name, input_data, ...):
    pkg = self._require_package(task_name, EntityKind.TASK)
    run_id = uuid4()
    assembler = self._new_assembler(pkg, run_id, ...)

    # 1. Resolve sources (Postgres, S3, etc.) before the model is called.
    context, src_audit = await self.binder.resolve_sources(pkg.sources, input_data, mock)
    for a in src_audit:
        assembler.add_source_resolution(a)

    # 2. Build the opening user message.
    user = render_template(pkg.prompt_template or "{{input}}", {"input": input_data, "context": context})
    messages = [_first_user_message(pkg, user, input_data, context)]

    # 3. Force structured output if a schema is declared.
    force_tool = None
    tools = []
    if pkg.output_schema:
        tools = [ToolDef(name="structured_output", description="...",
                         input_schema={"type": "object", "properties": pkg.output_schema})]
        force_tool = "structured_output"

    # 4. One model call.
    quota = QuotaEnforcer(max_turns=1, ...)
    quota.check_turn()
    response = await self._model_call(pkg, system, messages, tools, force_tool, depth)
    quota.record(response.usage, response.model)

    # 5. Extract the output.
    if force_tool:
        output = response.tool_calls[0].input if response.tool_calls else {}
    else:
        output = _try_json(response.text)

    # 6. Write targets.
    tgt_audit = await self.binder.write_targets(pkg.targets, output, input_data, str(run_id), mock)

    # 7. Finalize.
    return await self._finish(assembler, output, ...)
```

**Steps 1–2** are the same for tasks and agents — sources first, then the user
message. **Steps 3–5** are task-specific: instead of a loop, there is exactly one
model call, and the model is forced to call a synthetic `structured_output` tool
whose input *becomes* the run's output. **Steps 6–7** are again shared.

### Why force a tool for structured output?

Without tool-forcing, asking a model to "return JSON" produces unreliable results.
The model might wrap the JSON in a markdown code block, add a preamble, or omit
fields. Forcing the model to call `structured_output` (using the provider's
`tool_choice` API) guarantees the output matches the declared schema — the model
*cannot* return free text when a forced tool is active.

For **tasks** the forced tool is `structured_output`. For **agents** the synthetic
tool is `submit_output`, and it is not forced — instead the model chooses to call
it when it is ready to commit its final answer (ending the loop).

---

## The first user message

Before either path calls a model, it calls `_first_user_message`:

```python
def _first_user_message(pkg, user, fallback_input, src_ctx) -> Message:
    text = user if isinstance(user, str) else json.dumps(fallback_input)
    blocks: list = [TextBlock(text=text)]

    for src in pkg.sources:
        if not src.as_block:
            continue
        meta = src_ctx.get(src.bind_to)
        if not isinstance(meta, dict) or "_as_block" not in meta:
            continue
        if meta["_as_block"] == "image":
            blocks.append(ImageBlock(media_type=meta["_media_type"],
                                     data_b64=meta["_data_b64"], ...))
        elif meta["_as_block"] == "document":
            blocks.append(DocumentBlock(media_type=meta["_media_type"],
                                        data_b64=meta["_data_b64"], ...))

    return Message(role="user", content=blocks)
```

The first user message always starts with a `TextBlock` (the rendered prompt
template). If any source had `as_block: image` or `as_block: document`, the
corresponding binary content is appended as an `ImageBlock` or `DocumentBlock`.
The provider adapter then translates these to the native wire format:

```
First user message (IR):
  [TextBlock("Classify the attached document.\ndocument_key: documents/complaint.txt"),
   DocumentBlock(media_type="text/plain", data_b64="U3ViamVjd...")]

→ Anthropic API wire format:
  {"role": "user", "content": [
    {"type": "text", "text": "Classify the attached document. ..."},
    {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                    "data": "Subject: URGENT complaint..."}}
  ]}
```

Text sources (`required` Postgres rows, S3 objects via `get_object`) go into the
*prompt template* as text via `{{context.x}}`. Binary sources (`as_block` with
`get_bytes`) go into *content blocks* attached to the message. The model sees
both, but they reach it by different channels.

---

## The agent path

```python
async def run_agent(self, *, agent_name, context, ...):
    pkg = self._require_package(agent_name, EntityKind.AGENT)
    run_id = uuid4()
    assembler = self._new_assembler(pkg, run_id, ...)

    src_ctx, src_audit = await self.binder.resolve_sources(pkg.sources, context, mock)
    # ...
    merged = {**context, **src_ctx}
    user = render_template(pkg.prompt_template or "{{input}}", {"input": context, "context": src_ctx})
    messages = [_first_user_message(pkg, user, merged, src_ctx)]

    quota = QuotaEnforcer(max_turns=pkg.inference.max_turns, ...)
    return await self._agent_loop(pkg=pkg, ..., messages=messages, quota=quota, start_turn=0)
```

`run_agent` does source resolution, builds the first message, then hands off to
`_agent_loop`. It does not write targets or finalize the decision log directly —
that happens inside `_agent_loop` after the loop ends.

---

## The loop — `_agent_loop` stage by stage

`_agent_loop` is the heart of the engine. Here is every stage, in order:

```python
async def _agent_loop(self, *, pkg, run_id, ..., messages, quota, start_turn, ...):
    # Build the tool menu: package's declared tools + optional submit_output.
    tool_defs = [ToolDef(name=t.name, ...) for t in pkg.tools]
    if pkg.output_schema:
        submit_tool = ToolDef(name="submit_output", ...)
        tool_defs = [*tool_defs, submit_tool]

    submit_input = None
    turn = start_turn

    while True:
        # STAGE 1: Quota check.
        quota.check_turn()              # raises QuotaExceeded if over limit

        # STAGE 2: Model call.
        response = await self._model_call(pkg, pkg.system_prompt, messages, tool_defs, None, depth)
        quota.record(response.usage, response.model)
        assembler.add_turn(turn=turn, response=response, ...)
        messages.append(Message.assistant_blocks(response.blocks))

        # STAGE 3: Stop-reason check.
        if response.stop_reason != StopReason.TOOL_USE:
            status = RunStatus.COMPLETE
            break

        # STAGE 4: submit_output check.
        if submit_tool:
            for tc in response.tool_calls:
                if tc.name == "submit_output":
                    submit_input = tc.input
                    break
            if submit_input:
                status = RunStatus.COMPLETE
                break

        # STAGE 5: HITL gate — fires BEFORE executing a gated tool.
        for tc in response.tool_calls:
            if self.tools.needs_approval(tc.name, pkg.hitl):
                cont = Continuation(messages=[m.model_dump() for m in messages], ...)
                await self.continuations.save(cont)
                raise HITLSuspended(cont.id, run_id, tc.name)

        # STAGE 6: Normal tool dispatch.
        results = []
        for tc in response.tool_calls:
            rec = await self.tools.call(name=tc.name, tool_input=tc.input, ...)
            assembler.add_tool_call(rec)
            results.append(ToolResultBlock(tool_use_id=tc.id,
                                           content=rec["output_data"],
                                           is_error=rec["error"]))
        messages.append(Message.tool_results(results))
        turn += 1

    # After the loop:
    output = submit_input or _try_json(last_response.text) or {...}
    tgt_audit = await self.binder.write_targets(pkg.targets, output, ...)
    assembler.set_messages(messages)
    return await self._finish(assembler, output, status, ...)
```

Each stage is examined in detail below.

---

### Stage 1: Quota enforcement

```python
quota.check_turn()   # inside QuotaEnforcer
```

`QuotaEnforcer` tracks three limits declared in the package's `inference` config:

| Field | What it limits |
|---|---|
| `max_turns` | Maximum loop iterations |
| `max_total_tokens` | Total input + output tokens across all turns |
| `max_usd` | Total estimated cost in USD |

`check_turn()` raises `QuotaExceeded` before the model is called. The loop catches
it and exits with `RunStatus.MAX_TURNS`. The model is never called past its turn
budget — even if it is mid-tool-call, the next turn check stops it.

After the model responds, `quota.record(response.usage, response.model)` tallies
the actual tokens and estimated cost for that turn. Simple cost estimation: the
enforcer knows approximate per-token prices for known models and multiplies.

**Why enforce before the call, not after?** Because you cannot un-spend tokens. A
post-call check would still bill you for the over-limit turn. A pre-call check
ensures the ceiling is never exceeded.

---

### Stage 2: The model call

```python
response = await self._model_call(pkg, system, messages, tool_defs, force_tool, depth)
```

`_model_call` is a thin wrapper over `self.chain.complete(...)`, which is the
`ModelChain` defined in `harness/providers/base.py`. The chain:

1. Picks the first provider in priority order that has an API key registered.
2. Calls it with the neutral messages and tools.
3. If the call raises a `RetryableProviderError` (429, 5xx, timeout), backs off
   with full jitter and retries up to `max_retries` times.
4. If retries are exhausted — or a `FatalProviderError` fires (bad request, auth
   failure) — falls through to the next chain link (if fallback is enabled).
5. Raises `ProviderError` if every link fails.

The loop never sees which provider answered. `response.provider` tells you after
the fact (for the audit log), but the loop code is identical regardless.

The full message history is sent on every call. LLM APIs are stateless — the
provider has no memory of previous turns. `messages` grows by two entries per
turn (one assistant block, one user block carrying tool results), and the entire
list is sent each time.

---

### Stage 3: Stop-reason check

```python
if response.stop_reason != StopReason.TOOL_USE:
    status = RunStatus.COMPLETE
    break
```

The model can stop for three reasons:

| `stop_reason` | What it means | Loop action |
|---|---|---|
| `END` | Model is done — no more tools wanted | Exit, output is the text or `submit_output` input |
| `TOOL_USE` | Model wants tools — keep going | Continue to stages 4–6 |
| `MAX_TOKENS` | Response was truncated | Exit as COMPLETE (partial output) |
| `OTHER` | Unexpected stop | Exit as COMPLETE |

`END` without a `submit_output` call means the model finished by writing free
text. The engine still captures this via `_try_json(last_response.text)`, which
tries to parse JSON from the text and falls back to `{"raw_output": text}`.

---

### Stage 4: submit_output terminator

```python
for tc in response.tool_calls:
    if tc.name == "submit_output":
        submit_input = tc.input
        break
if submit_input:
    status = RunStatus.COMPLETE
    break
```

When `output_schema` is declared, a synthetic `submit_output` tool is added to
the model's tool menu. Its `input_schema` is the declared schema. The model calls
it when it has its final answer — the tool's `input` dict *is* the output.

This is checked **before** HITL and tool dispatch because `submit_output` is a
terminator: once called, the run is done. The loop does not execute any other
tools from the same turn, even if the model listed them.

`submit_output` never reaches the tool gateway. The loop intercepts it here and
exits. There is no Python function registered for `submit_output`.

---

### Stage 5: HITL gate

```python
for tc in response.tool_calls:
    if self.tools.needs_approval(tc.name, pkg.hitl):
        cont = Continuation(
            run_id=run_id,
            messages=[m.model_dump() for m in messages],
            turn=turn,
            pending_tool_use={"id": tc.id, "name": tc.name, "input": tc.input},
            ...
        )
        await self.continuations.save(cont)
        raise HITLSuspended(cont.id, run_id, tc.name)
```

`needs_approval` checks whether `tc.name` is in `pkg.hitl.require_approval_for`
and `pkg.hitl.enabled` is True.

When a gated tool is found:

1. **The full loop state is serialized** into a `Continuation` object: every
   message in the conversation so far, the turn number, all tool calls recorded,
   the pending tool call that needs approval, the run's mock context.

2. **The continuation is saved** to the store (`FileContinuationStore` writes a
   JSON file; a Postgres store could write a row).

3. **`HITLSuspended` is raised.** This propagates up through `run_agent` to the
   worker, which marks the run `suspended` in Postgres and releases the worker
   process. No thread is held waiting for the human.

The model's *other* tool calls in the same turn (if any) are not executed —
the gate fires as soon as the first gated tool is found. This is correct: you
cannot execute un-approved tool calls before the human reviews the gated one.

**Scope constraint:** HITL gates only fire at `depth == 0` (top-level agents).
If a sub-agent (depth > 0) requests a gated tool, the engine instead returns a
denied `tool_result` to the sub-agent with an explanatory error. The reason:
the parent agent is synchronously waiting for the sub-agent to return — pausing
the sub-agent for human-time would block the parent forever.

---

### Stage 6: Normal tool dispatch

```python
results = []
for tc in response.tool_calls:
    rec = await self.tools.call(
        name=tc.name, tool_input=tc.input, authorized=pkg.tools,
        call_order=..., mock=mock,
        delegate_ctx=self._delegate_ctx(pkg, ...)
    )
    assembler.add_tool_call(rec)
    results.append(ToolResultBlock(
        tool_use_id=tc.id,
        content=rec["output_data"],
        is_error=rec["error"],
    ))
messages.append(Message.tool_results(results))
```

`self.tools.call` is the **tool gateway** (`harness/tools/gateway.py`). It:
1. Checks that `tc.name` is in `pkg.tools` (authorization — non-bypassable)
2. Routes to the right transport: `python_inprocess` / `mcp_http` / `mcp_stdio` / `verity_builtin`
3. Returns a `tool_record` dict with `output_data` and `error` fields

Every tool — whether Python, MCP, or built-in — returns the same dict shape. The
loop wraps it in a `ToolResultBlock` using `tc.id` for correlation (so the model
knows which of its requests is being answered).

Multiple tool calls from one model turn are dispatched sequentially in this
implementation. The results are collected and sent back to the model together as
a single user-role message with multiple `ToolResultBlock`s.

The gateway is covered in full in **Module 06**. One detail worth knowing now:
`delegate_to_agent` is the `verity_builtin` tool. When the model calls it, the
gateway routes to `self._delegate`, which re-enters `run_agent` at depth+1. The
sub-agent is a fully governed nested run — it resolves its own sources, runs its
own loop, writes its own decision record, and returns its output as the tool
result. From the parent loop's perspective, delegation is just another tool call
that happens to be slow.

---

## After the loop

When the loop exits (any terminal state), `_agent_loop` writes targets and
finalizes:

```python
# Select the output.
if submit_input is not None:
    output = submit_input                          # from submit_output tool call
elif last_response.stop_reason != StopReason.TOOL_USE:
    output = _try_json(last_response.text)         # parse free text
else:
    output = {"status": status.value, "note": "loop ended without a final answer"}

# Write targets (S3, etc.).
tgt_audit = await self.binder.write_targets(pkg.targets, output, run_input, str(run_id), mock)

# Commit the full conversation to the assembler.
assembler.set_messages(messages)

# Write the decision log and return.
return await self._finish(assembler, output, status, ...)
```

Targets are written **after** the loop exits, not during it. This is intentional:
no partial results are written if the loop fails mid-run. The output is either the
`submit_output` input (for schema-enforced agents), the model's final text
(for free-form agents), or a synthetic failure dict.

`_try_json` attempts to parse the model's final text as JSON, strips markdown
fences if present, and falls back to `{"raw_output": text}` if parsing fails.

---

## `_finish` — the decision record

```python
async def _finish(self, assembler, output, reasoning, status, error, duration, run_id, depth):
    rec = assembler.finish(output=output, reasoning=reasoning, status=status.value,
                           error=error, duration_ms=duration)
    decision_id = await self.sink.write(assembler)
    ...
    return ExecutionResult(
        run_id=run_id, entity_kind=rec.entity_kind, entity_name=rec.entity_name,
        status=status, output=output, decision_log_id=decision_id,
        duration_ms=duration, usage=_usage_from(rec),
    )
```

`assembler.finish` stamps the end time and seals the record. `self.sink.write`
persists it — either as `decision_log.json` + `model_invocations.jsonl` under
`./_artifacts/runs/{date}/{run_id}/` (FileSink, the default), or as a row in
`agent_decision_log` (PostgresSink, when `DECISION_SINK=postgres`).

The returned `ExecutionResult` is what the caller sees (the CLI, the worker, the
notebook). It carries the output, the decision log id, token counts, and duration
— enough to report back to the user and link to the full governance record.

---

## Resume — rebuilding from a checkpoint

When a HITL gate fires, the loop serializes everything and raises. Later, when
the human records a decision, `resume` is called:

```python
async def resume(self, suspension_id: UUID) -> ExecutionResult:
    # 1. Load the serialized loop state.
    cont = await self.continuations.load(suspension_id)

    # 2. Rebuild messages from the checkpoint (neutral IR — not vendor format).
    messages = [Message.model_validate(m) for m in cont.messages]

    # 3. Restore the assembler with what was already accumulated.
    assembler = self._new_assembler(pkg, cont.run_id, ...)
    for tc in cont.tool_calls_made:
        assembler.add_tool_call(tc)

    # 4. Apply the human decision to the pending tool call.
    decision = cont.decision
    pending  = cont.pending_tool_use

    if decision.decision == "approve":
        rec = await self.tools.call(name=pending["name"], tool_input=pending["input"], ...)
    elif decision.decision == "edit":
        rec = await self.tools.call(name=pending["name"], tool_input=decision.edited_input, ...)
    else:  # deny
        rec = {"output_data": {"error": "Tool call denied..."}, "error": True, ...}

    # 5. Inject the result as a tool_result message.
    messages.append(Message.tool_results([ToolResultBlock(
        tool_use_id=pending["id"], content=rec["output_data"], is_error=rec["error"])]))

    # 6. Re-enter the loop from turn+1.
    return await self._agent_loop(
        pkg=pkg, messages=messages, assembler=assembler,
        start_turn=cont.turn + 1, ...
    )
```

The key insight: **the messages list is stored in neutral IR** (JSON-serialisable
pydantic models). When `resume` loads them with `Message.model_validate(m)`, it
gets back proper `Message` objects with typed blocks — `TextBlock`, `ToolUseBlock`,
`ToolResultBlock`, `ImageBlock`, `DocumentBlock` — all in the same shape the model
gateway expects. No re-parsing, no vendor-specific decoding.

The human has three choices:

| Decision | What happens |
|---|---|
| `approve` | The tool executes with the original inputs |
| `edit` | The tool executes with the reviewer's modified inputs |
| `deny` | A `ToolResultBlock(is_error=True)` is injected; the model decides how to recover |

In all three cases, `_agent_loop` continues from `turn + 1` with the result in
the conversation. The model sees a normal tool result and continues reasoning. It
does not know whether the human approved, edited, or denied — it only sees the
outcome.

---

## The decision assembler — what gets recorded

The `DecisionAssembler` accumulates state throughout a run. Every stage that
matters calls into it:

```python
assembler.add_source_resolution(a)   # every source fetch (pg, s3, mocked)
assembler.add_turn(turn, response)   # every model response + raw blocks
assembler.add_tool_call(rec)         # every tool call (python, mcp, builtin, hitl)
assembler.add_target_write(a)        # every target write (s3, suppressed)
assembler.set_messages(messages)     # full conversation after the loop
assembler.finish(output, ...)        # seals the record
```

The resulting `decision_log.json` contains 31 fields including:

```
id                     — decision log UUID
entity_kind / name     — "task"/"agent" + package name
input_json             — the run's input dict
output_json            — the run's final output
model_chain            — all providers configured (not just the one used)
models_used            — which model(s) actually responded
input_tokens           — total across all turns
output_tokens          — total across all turns
tool_calls_made        — every tool: name, input, output, transport, error?
source_resolutions     — every source: connector, ref, value preview, mocked?
target_writes          — every write: connector, container, success?
message_history        — full conversation (all turns, all blocks)
status                 — complete / failed / suspended / max_turns
decision_depth         — 0=top-level, 1=first sub-agent, etc.
parent_decision_id     — links sub-agent records to parent
hitl_required          — whether the package has HITL enabled
```

The `model_invocations.jsonl` alongside it contains one JSON line per model
response — the raw blocks returned by the model, in IR form. This is what
`harness.cli reproduce` reads to replay the run bit-exactly without calling
the real API.

---

## What the engine does NOT know about

By design, the engine imports nothing from `anthropic`, `openai`, or
`google.genai`. It only imports from:

- `harness.core.ir` — the neutral types
- `harness.core.package` — the package schema
- `harness.providers.base` — `ModelChain` (which calls the adapters)
- `harness.tools.gateway` — `ToolGateway`
- `harness.connectors.binder` — `Binder`
- `harness.decisions.assembler` — `DecisionAssembler`
- `harness.hitl.continuation` — `Continuation`, `FileContinuationStore`
- `harness.quota.enforcer` — `QuotaEnforcer`
- `harness.mock.context` — `MockContext`

This is not accidental. Every vendor-specific concern is pushed to the adapters.
If Anthropic changes their API, only `anthropic_provider.py` changes — the engine
is unaffected. If a new provider is added, a new adapter is written and registered
in the factory — the engine still runs unchanged.

---

## The mock seam

Every path in the engine passes a `mock: Optional[MockContext]` argument. The
binder and tool gateway check this at every gate:

```python
# In binder.resolve_sources:
if mock is not None and src.bind_to in mock.source_responses:
    value = mock.source_responses[src.bind_to]  # ← canned value, skip real fetch

# In ToolGateway.call:
if mock is not None and tc.name in mock.tool_responses:
    return mock.tool_responses[tc.name]          # ← canned result, skip real dispatch

# In binder.write_targets:
if mock is not None and mock.suppress_targets:
    ...  # ← audited no-op, skip real write
```

And `self.chain.complete` uses the `MockProvider` (which replays scripted turns)
when the chain's first link resolves to `mock`.

`MockContext` is a single object that flips all four gateways (model, tool, source,
target) without touching the engine. The same `_agent_loop` code runs; only the
side effects change. This is what makes the full offline test suite possible with
no API keys, no Postgres, no MinIO, no MCP server.

---

## Putting it together — one full run, traced

Here is what happens, event by event, when you run:

```bash
python -m harness.cli demo underwriting_bind --auto-approve
```

```
RUN_STARTED      entity=underwriting_agent  kind=agent

SOURCE_RESOLVED  bind_to=submission  connector=pg_main  mocked=false
                 → row: {named_insured: "Lakeview Bistro LLC", tiv: 1850000, ...}

TURN_STARTED     turn=0
MODEL_ATTEMPT    provider=anthropic  model=claude-opus-4-8
MODEL_RESPONDED  stop=tool_use  tools=1  (delegate_to_agent → loss_history_analyst)

TOOL_CALLED      tool=delegate_to_agent  depth=0
  RUN_STARTED    entity=loss_history_analyst  kind=agent  depth=1
  TURN_STARTED   turn=0
  MODEL_RESPONDED stop=tool_use  (pull_loss_runs)
  TOOL_CALLED    tool=pull_loss_runs  transport=mcp_http
  TOOL_RESULT    → {loss_count:1, loss_ratio:0.15, ...}
  TURN_STARTED   turn=1
  MODEL_RESPONDED stop=tool_use  (submit_output)
  DECISION_LOGGED depth=1  status=complete
  RUN_COMPLETE   depth=1
TOOL_RESULT      output_data={sub_status:complete, output:{loss_count:1, ...}}

TURN_STARTED     turn=1
MODEL_RESPONDED  stop=tool_use  (lookup_appetite)
TOOL_CALLED      tool=lookup_appetite  transport=mcp_http
TOOL_RESULT      → {in_appetite:true, authority_tiv:3000000, authority_premium:15000}

TURN_STARTED     turn=2
MODEL_RESPONDED  stop=tool_use  (property_data)
TOOL_CALLED      tool=property_data  transport=mcp_http
TOOL_RESULT      → {wind_zone:"none", distance_to_coast_mi:800, protection_class:4}

TURN_STARTED     turn=3
MODEL_RESPONDED  stop=tool_use  (rate_property)
TOOL_CALLED      tool=rate_property  transport=python_inprocess
TOOL_RESULT      → {premium:7438.18, cope_factors:{...}}

TURN_STARTED     turn=4
MODEL_RESPONDED  stop=tool_use  (bind_policy)

HITL_GATE        tool=bind_policy
HITL_SUSPENDED   suspension_id=<uuid>
  # --auto-approve: records decision, calls resume()
HITL_RESUMED     gate=bind_policy  decision=approve

TOOL_CALLED      tool=bind_policy  transport=python_inprocess
TOOL_RESULT      → {binder_number:"BND-0001-...", status:"bound"}

TURN_STARTED     turn=5
MODEL_RESPONDED  stop=tool_use  (submit_output)
  → {decision:"bound", premium:7438.18, ...}

TARGET_WRITTEN   connector=s3_main  container=underwriting/<run_id>.json
DECISION_LOGGED  depth=0  status=complete
RUN_COMPLETE     status=complete  tokens=...
```

Three decision records are written:
1. `loss_history_analyst` at depth 1 (written when it completes, before the parent)
2. `underwriting_agent` at depth 0 (initial run, suspended mid-loop)
3. `underwriting_agent` at depth 0 (resumed run, completes here)

---

## Checkpoint

1. `run_task` makes exactly one model call. `run_agent` may make many. What is
   the structural reason they share `_first_user_message` but diverge after that?
2. Why does the `submit_output` check come **before** the HITL gate check?
3. A model returns `stop_reason: END` without calling `submit_output`, even though
   the package declares an `output_schema`. What does the engine produce as output?
4. When `resume` loads a continuation, it reconstructs `messages` from the stored
   dicts using `Message.model_validate`. Why is storing neutral IR (not
   Anthropic/Gemini format) essential here?
5. A sub-agent at depth 1 tries to call `bind_policy`, which is in its parent's
   HITL policy. What does the engine do, and why?
6. The engine sends the full message history to the model on every turn. Why can
   it not send only the most recent message?

When you can answer these, move to **[Module 05: Providers and the Model Chain](05-providers-and-chain.md)** —
how the chain resolves which provider to call, how retry and fallback work, and
exactly what each adapter does to translate between IR and its vendor's wire
format.