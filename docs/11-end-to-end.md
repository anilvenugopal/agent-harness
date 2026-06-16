# Module 11 — End-to-End Walkthrough

> This module traces a single run of `python -m harness.cli demo underwriting_bind
> --auto-approve` from command invocation to the final decision records. Every
> layer covered in Modules 01–10 is touched. Use this as a map to navigate back
> to the relevant module for any stage you want to understand more deeply.

---

## The scenario

The `underwriting_bind` demo submits **Lakeview Bistro LLC** (Chicago restaurant,
TIV $1.85M) for a commercial property policy. The expected path:

1. Agent delegates to `loss_history_analyst` → 1 prior loss, low ratio, acceptable
2. `lookup_appetite` → in appetite, TIV within authority
3. `property_data` → no coastal exposure, PPC 4 (good)
4. `rate_property` → computed annual premium ~$7,438
5. `bind_policy` → HITL gate fires → auto-approved → binder issued
6. `submit_output` → `{decision: "bound", premium: 7438.18, ...}`

Three decision records are written: `loss_history_analyst` (depth 1),
`underwriting_agent` partial (suspended), `underwriting_agent` resumed (complete).

---

## Full sequence at a glance

The diagram below shows every significant call boundary for `underwriting_bind
--auto-approve`. Read top-to-bottom. Indentation shows call depth.

```
CLI
│  load_dotenv()
│  build_engine(packages, providers, connectors, tools, mcp, sink, ...)
│
└─► engine.run_agent("underwriting_agent", {"submission_id": 1})
    │
    ├─► binder.resolve_sources()
    │     └─► pg_main.fetch("query_one", "SELECT * FROM submission WHERE id=1")
    │           └── returns {named_insured: "Lakeview Bistro LLC", tiv: 1850000, ...}
    │
    ├─► _first_user_message()  → Message[TextBlock(rendered prompt)]
    │
    └─► _agent_loop(start_turn=0)
        │
        ├── TURN 0 ──────────────────────────────────────────────────────
        │   quota.check_turn()  ✓
        │   chain.complete() → AnthropicProvider → API call
        │     response: ToolUseBlock(delegate_to_agent, {agent_name: "loss_history_analyst"})
        │   assembler.add_turn(0, response)
        │   HITL: delegate_to_agent not gated
        │   gateway.call("delegate_to_agent") → engine._delegate()
        │   │
        │   └─► engine.run_agent("loss_history_analyst", {...}, depth=1)
        │       ├─► binder.resolve_sources() — no sources
        │       └─► _agent_loop(depth=1)
        │           ├── TURN 0: pull_loss_runs (MCP → policy_service)
        │           │     mcp_client.ensure_open("policy_service")  [lazy open]
        │           │     call_tool("pull_loss_runs", ...) → {loss_count:1, ...}
        │           └── TURN 1: submit_output({loss_count:1, loss_ratio:0.15, ...})
        │               [loop exits; decision record written at depth=1]
        │               returns ExecutionResult(status=complete)
        │
        │   tool_record = {output_data: {sub_status:"complete", output:{...}}}
        │   assembler.add_tool_call(tool_record)
        │   messages += [ToolResultBlock(delegation result)]
        │
        ├── TURN 1 ──────────────────────────────────────────────────────
        │   chain.complete() → ToolUseBlock(lookup_appetite, {...})
        │   gateway.call("lookup_appetite") → mcp_client.call_tool(...)
        │     → {in_appetite:true, authority_tiv:3000000}
        │   messages += [ToolResultBlock(appetite)]
        │
        ├── TURN 2 ──────────────────────────────────────────────────────
        │   chain.complete() → ToolUseBlock(property_data, {...})
        │   gateway.call("property_data") → mcp_client.call_tool(...)
        │     → {wind_zone:"none", protection_class:4, ...}
        │   messages += [ToolResultBlock(property data)]
        │
        ├── TURN 3 ──────────────────────────────────────────────────────
        │   chain.complete() → ToolUseBlock(rate_property, {...})
        │   gateway.call("rate_property") → python_tools.call(rate_property, ...)
        │     → {premium:7438.18, cope_factors:{...}}
        │   messages += [ToolResultBlock(rating)]
        │
        ├── TURN 4 ──────────────────────────────────────────────────────
        │   chain.complete() → ToolUseBlock(bind_policy, {premium:7438.18, ...})
        │   HITL CHECK: bind_policy IS gated
        │   continuations.save(Continuation(messages[0..8], turn=4, ...))
        │                      → suspensions/{uuid}.json
        │   raise HITLSuspended(suspension_id, ...)
        │
├── HITLSuspended caught by CLI
│   print "⏸  SUSPENDED"
│   --auto-approve:
│   continuations.record_decision(suspension_id, HumanDecision("approve"))
│
└─► engine.resume(suspension_id)
    ├─► continuations.load(suspension_id)
    │     → Continuation with 9 messages and pending bind_policy call
    ├─► Message.model_validate(m) for m in cont.messages  → 9 typed Messages
    ├─► restore assembler (prior tool records)
    ├─► decision="approve" → gateway.call("bind_policy", original_input)
    │     python_tools.call(bind_policy, ...) → {binder_number:"BND-0001-...", status:"bound"}
    ├─► messages += [ToolResultBlock(bind result)]
    │
    └─► _agent_loop(start_turn=5)
        │
        └── TURN 5 ──────────────────────────────────────────────────────
            chain.complete() → ToolUseBlock(submit_output, {decision:"bound", premium:7438.18, ...})
            submit_output check: loop exits, status=COMPLETE
        │
        ├─► binder.write_targets()
        │     s3_main.write("put_object", "underwriting/{run_id}.json", output)
        │       → {bucket:"underwriting", key:"{run_id}.json", etag:"..."}
        │
        ├─► assembler.set_messages(all 11 messages)
        ├─► assembler.finish(output, status="complete", duration_ms=...)
        └─► sink.write(assembler)
              _artifacts/runs/2026/06/15/{run_id}/decision_log.json
              _artifacts/runs/2026/06/15/{run_id}/model_invocations.jsonl  (6 lines)

CLI: _print_result(res)
```

---

## Stage 0: CLI startup

```
python -m harness.cli demo underwriting_bind --auto-approve
```

`harness/cli.py` runs. Before anything else, `load_dotenv()` pulls `.env` into
the process environment so `ANTHROPIC_API_KEY`, `S3_ENDPOINT_URL`, etc. are
available without the user having manually `source`d the file. (`override=False`
means shell-exported variables take precedence over the file.)

The CLI prints:

```
── demo: underwriting_bind ──
  package : underwriting_agent  (agent)
  input   : {"submission_id": 1}
```

Then builds the engine via `build_engine(...)`:
- `load_packages("packages/")` → loads all three YAML files
- `build_providers()` → registers `anthropic`, `openai`, `gemini`, `mock` (key-gated)
- `build_connectors()` → registers `pg_main` (if `PG_MAIN_DSN`) and `s3_main`
- `build_demo_tools()` → registers `rate_property` and `bind_policy`
- `MCPClient(_mcp_servers())` → configures `policy_service` (stdio or HTTP)
- `FileSink`, `FileContinuationStore`, `Tracer(enabled=True, step=False)`

**Module cross-refs:** Factory wiring (Module 05 § factory), connector registration
(Module 07 § registry), MCP client (Module 06 § mcp transport).

---

## Stage 1: `run_agent` entry

```python
await engine.run_agent(agent_name="underwriting_agent", context={"submission_id": 1})
```

**Source resolution** (Module 07):

The package declares one source:
```yaml
sources:
  - connector: pg_main
    method: query_one
    ref: "SELECT * FROM submission WHERE id = {{input.submission_id}}"
    bind_to: submission
    required: true
```

`Binder.resolve_sources` templates the ref:
```sql
SELECT * FROM submission WHERE id = 1
```
Calls `pg_main.fetch("query_one", sql)`. Returns the Lakeview Bistro row as a dict.
Stores it in `context["submission"]`. Adds a source audit record to the assembler.

**Prompt rendering:**
```
Underwrite this commercial property submission:
submission_id: 1

Submission record from system of record:
{'id': 1, 'named_insured': 'Lakeview Bistro LLC', 'tiv': 1850000, ...}
```

**First user message** (Module 04 `_first_user_message`): `TextBlock` only — no
`as_block` sources for the agent. The message list starts with one `Message(role="user", content=[TextBlock(...)])`.

---

## Stage 2: Turn 0 — model call, model requests delegation

`_agent_loop` starts at `turn=0`.

**Quota check** (Module 04 Stage 1): `QuotaEnforcer(max_turns=10, max_usd=2.00)`.
Turn 0 is within budget. `_turns` increments to 1.

**Model call** (Module 05): `ModelChain.complete` picks Anthropic (priority 0).
`AnthropicProvider.complete` translates the IR message to Anthropic wire format,
calls `client.messages.create(...)`. The model responds:

```
stop_reason: tool_use
blocks: [ToolUseBlock(id="toolu_01...", name="delegate_to_agent",
                      input={"agent_name": "loss_history_analyst",
                             "context": {"submission_id": 1, ...}})]
```

`assembler.add_turn(turn=0, response=...)` accumulates tokens. The model response
goes into `model_invocations[0]`.

**Stop reason check**: `TOOL_USE` → continue.
**`submit_output` check**: not called.
**HITL check**: `delegate_to_agent` is not in `require_approval_for`. No suspension.

**Tool dispatch** (Module 06):

```python
rec = await gateway.call(name="delegate_to_agent", tool_input={...}, ...)
```

1. Auth: `authorize("delegate_to_agent", pkg.tools)` → authorized.
2. Mock: no MockContext → not mocked.
3. Transport: `verity_builtin` → `gateway._builtin` → `engine._delegate`.

---

## Stage 3: Delegation — `loss_history_analyst` runs

`engine._delegate` checks:
- `child_name = "loss_history_analyst"` is a valid string
- `child_context = {"submission_id": 1, ...}` is a dict
- depth 0 + 1 = 1 < `MAX_DECISION_DEPTH`
- `loss_history_analyst` is in `underwriting_agent`'s `delegations`

Calls `engine.run_agent(agent_name="loss_history_analyst", context={...}, depth=1, mock=None)`.

**Child run:**
- No sources. First user message: `TextBlock("Analyze loss history for...")`.
- Turn 0: model requests `pull_loss_runs` (MCP tool).
- MCP dispatch: `gateway._mcp` → `mcp_client.ensure_open("policy_service")` (first
  use → opens the stdio/HTTP connection) → `call_tool("policy_service", "pull_loss_runs", {...})`.
- MCP returns: `{loss_count: 1, gross_incurred: 285000, loss_ratio: 0.154, ...}`
- Turn 1: model calls `submit_output` → `{sub_status: "complete", loss_count: 1, loss_ratio: 0.15, recommendation: "acceptable", ...}`.
- Loop exits. `assembler.set_messages(...)`. `_finish(...)`.
- **Child decision record written** (depth=1, `parent_decision_id` = parent's assembler ID).
- `run_agent` returns `ExecutionResult(status=COMPLETE, output={...})`.

Back in `engine._delegate`, the result becomes:
```python
{
    "tool_name": "delegate_to_agent", "transport": "verity_builtin",
    "output_data": {
        "sub_decision_log_id": "7f3a...",
        "sub_status": "complete",
        "output": {"loss_count": 1, "loss_ratio": 0.15, ...},
    }
}
```

This goes into the parent's `tool_calls_made`. The parent's loop appends a
`ToolResultBlock` to its `messages` list and increments `turn` to 1.

**Module cross-refs:** Delegation governance (Module 06 § delegation), MCP client
(Module 06 § mcp transport), child decision records (Module 09 § tree).

---

## Stages 4–6: Turns 1–3 — appetite, property data, rating

Each turn follows the same pattern: model call → `TOOL_USE` stop → gateway
dispatch → tool result → next turn.

**Turn 1 — `lookup_appetite`** (MCP):
Model calls `lookup_appetite(line="commercial_property", occupancy="restaurant",
construction="joisted_masonry", state="IL")`. MCP returns
`{in_appetite: true, authority_tiv: 3000000, authority_premium: 15000}`.

**Turn 2 — `property_data`** (MCP):
Model calls `property_data(submission_id=1, state="IL", ...)`. MCP returns
`{wind_zone: "none", distance_to_coast_mi: 800, protection_class: 4, flood_zone: "X"}`.

**Turn 3 — `rate_property`** (Python in-process):
Model calls `rate_property(tiv=1850000, occupancy="restaurant", construction="joisted_masonry",
protection_class=4, sprinklered=True, deductible=5000)`.

```python
# scripts/demo_app.py
base       = 0.0045    # restaurant
c_factor   = 1.10      # joisted_masonry
p_factor   = 0.95      # PPC 4
spk_factor = 0.90      # sprinklered
ded_factor = 0.95      # $5k deductible

premium = 1850000 * 0.0045 * 1.10 * 0.95 * 0.90 * 0.95
        = 7438.18
```

Result: `{premium: 7438.18, cope_factors: {base_rate: 0.0045, construction: 1.10, ...}}`.

After turn 3 the message list has 8 entries:
```
messages[0] = user: TextBlock (rendered prompt + submission data)
messages[1] = assistant: ToolUseBlock (delegate_to_agent)
messages[2] = user: ToolResultBlock (delegation result)
messages[3] = assistant: ToolUseBlock (lookup_appetite)
messages[4] = user: ToolResultBlock (appetite result)
messages[5] = assistant: ToolUseBlock (property_data)
messages[6] = user: ToolResultBlock (property data result)
messages[7] = assistant: ToolUseBlock (rate_property)
messages[8] = user: ToolResultBlock (premium = 7438.18)
```

---

## Stage 7: Turn 4 — HITL gate fires

The model's turn 4 response:

```
stop_reason: tool_use
blocks: [ToolUseBlock(id="toolu_05...", name="bind_policy",
                      input={submission_id: 1, premium: 7438.18,
                             limit: 1850000, deductible: 5000})]
```

**HITL check** (Module 08):
```python
if self.tools.needs_approval("bind_policy", pkg.hitl):   # → True
```

`Continuation` is built with all 9 messages (0–8 accumulated), all tool records,
turn=4, `gate_tool="bind_policy"`, `pending_tool_use={id: "toolu_05...", ...}`.

`await self.continuations.save(cont)` writes `./_artifacts/suspensions/{uuid}.json`.

`raise HITLSuspended(cont.id, run_id, "bind_policy")`.

The CLI catches it:

```
⏸  SUSPENDED for human approval: gate='bind_policy' suspension=<uuid>
   --auto-approve: recording approval and resuming...
```

```python
await engine.continuations.record_decision(
    s.suspension_id,
    HumanDecision(decision="approve", decided_by="demo")
)
res = await engine.resume(s.suspension_id)
```

**Module cross-refs:** Gate mechanics (Module 04 Stage 5), continuation store
(Module 08 § store), resume path (Module 08 § resume).

---

## Stage 8: Resume

`engine.resume(suspension_id)`:

1. Loads the continuation JSON → `Continuation` object.
2. `messages = [Message.model_validate(m) for m in cont.messages]` → 9 typed `Message` objects.
3. Restores assembler with prior tool records.
4. Decision is `"approve"` → calls `gateway.call(name="bind_policy", tool_input=pending_tool_use["input"], ...)`.
5. `bind_policy` runs: generates `BND-0001-XXXXXX`, returns `{binder_number: "BND-0001-...", status: "bound", ...}`.
6. Appends `ToolResultBlock` to messages (now 10 entries: messages[0–8] + bind_policy result).
7. Re-enters `_agent_loop` at `start_turn=5`.

---

## Stage 9: Turn 5 — submit_output

Model sees the bind confirmation and calls `submit_output`:

```
stop_reason: tool_use
blocks: [ToolUseBlock(id="mock_5_0", name="submit_output",
                      input={
                          decision: "bound",
                          premium: 7438.18,
                          coverage_summary: {limit: 1850000, deductible: 5000},
                          cope_factors: {base_rate: 0.0045, ...},
                          referral_reasons: [],
                          rationale: "Lakeview Bistro meets all appetite criteria..."
                      })]
```

`submit_output` is the terminator check (Module 04 Stage 4). `submit_input` is
set. Loop exits with `status = RunStatus.COMPLETE`.

---

## Stage 10: Target write and finalization

**Target write** (Module 07):

```yaml
targets:
  - connector: s3_main
    method: put_object
    from_path: "$"
    container: "underwriting/{{run_id}}.json"
    required: false
```

`Binder.write_targets`: selects `$` (whole output), templates the container with
the run UUID, calls `s3_main.write("put_object", "underwriting/<run_id>.json", output_dict)`.
S3Connector writes to MinIO. Returns `{bucket: "underwriting", key: "<run_id>.json", etag: "..."}`.

**`assembler.set_messages(messages)`** — all 11 messages serialised to neutral IR.
**`assembler.finish(output, status="complete", ...)`** — seals the record.
**`sink.write(assembler)`** — writes two files:

```
_artifacts/runs/2026/06/15/<run_id>/
    decision_log.json          ← 31 fields, all audit data
    model_invocations.jsonl    ← 6 lines (turns 0–5)
```

`ExecutionResult` is returned to the CLI. The CLI calls `_print_result(res)`.

---

## What was produced

The three decision log records and how they link:

```
_artifacts/runs/2026/06/15/

  7f3ab8c2-.../                        ← loss_history_analyst (depth=1)
  decision_log.json
    entity_name:        loss_history_analyst
    decision_depth:     1
    parent_decision_id: a2c1f3e7-...   ──────────────────────────┐
    status:             complete                                 │
    models_used:        [claude-haiku-4-5-20251001]              │
    tool_calls_made:    [{pull_loss_runs, mcp_http}]             │
    output_json:        {loss_count:1, loss_ratio:0.15}          │
                                                                 │
  a2c1f3e7-.../                        ← underwriting_agent (depth=0)
  decision_log.json                                              ▲
    entity_name:        underwriting_agent          linked by ───┘
    decision_depth:     0
    parent_decision_id: null
    status:             complete
    models_used:        [claude-opus-4-8]
    input_tokens:       (sum across all 6 turns)
    source_resolutions: [{pg_main, submission, mocked:false}]
    tool_calls_made:    [
      {delegate_to_agent, sub_decision_log_id: "7f3ab8c2-..."}, ◄── cross-link to child
      {lookup_appetite,   mcp_http},
      {property_data,     mcp_http},
      {rate_property,     python_inprocess},
      {bind_policy,       python_inprocess},
    ]
    target_writes:      [{s3_main, "underwriting/{run_id}.json", suppressed:false}]
    output_json:        {decision:"bound", premium:7438.18, ...}
  model_invocations.jsonl               ← 6 lines, one per turn
    {"turn":0, "model":"claude-opus-4-8", "blocks":[delegate call], ...}
    {"turn":1, "model":"claude-opus-4-8", "blocks":[lookup_appetite call], ...}
    ...
    {"turn":5, "model":"claude-opus-4-8", "blocks":[submit_output call], ...}

_artifacts/suspensions/

  {suspension_id}.json                  ← Continuation (status: resolved)
    gate_tool:          bind_policy
    turn:               4
    messages:           [9 neutral Message dicts]
    decision:           {decision:"approve", decided_by:"demo"}
```

Three decision log records:

```
loss_history_analyst  (depth=1)
  id: 7f3a...
  parent_decision_id: a2c1...
  status: complete
  models_used: ["claude-haiku-4-5-20251001"]
  tool_calls_made: [{tool_name: "pull_loss_runs", transport: "mcp_http", ...}]
  output: {loss_count: 1, loss_ratio: 0.15, recommendation: "acceptable"}

underwriting_agent / initial (depth=0) — was suspended, no record written
  [The continuation file contains the partial state]
  ./_artifacts/suspensions/<suspension_id>.json

underwriting_agent / resumed (depth=0)
  id: a2c1...
  parent_decision_id: null
  status: complete
  models_used: ["claude-opus-4-8"]
  tool_calls_made: [
    {tool_name: "delegate_to_agent", output_data: {sub_status: "complete", ...}},
    {tool_name: "lookup_appetite", transport: "mcp_http", ...},
    {tool_name: "property_data", transport: "mcp_http", ...},
    {tool_name: "rate_property", transport: "python_inprocess", ...},
    {tool_name: "bind_policy", transport: "python_inprocess", ...},
  ]
  source_resolutions: [{bind_to: "submission", connector: "pg_main", mocked: false}]
  target_writes: [{container: "underwriting/<run_id>.json", suppressed: false}]
  output: {decision: "bound", premium: 7438.18, ...}
```

One MinIO object:

```
bucket: underwriting
key:    <run_id>.json
body:   {decision: "bound", premium: 7438.18, ...}
```

---

## The classify path (for contrast)

The `classify` scenario is simpler — a single-turn task with no loop:

```
run_task("classify_document", {"document_key": "documents/complaint.txt"})
  └── resolve_sources
        └── s3_main.fetch("get_bytes", "documents/complaint.txt") → bytes
        └── binder wraps: {_as_block: "document", _media_type: "text/plain", _data_b64: "..."}
  └── _first_user_message
        └── TextBlock("Classify the attached document. document_key: documents/complaint.txt")
        └── DocumentBlock(media_type="text/plain", data_b64="...")
  └── one model call, force_tool="structured_output"
        └── AnthropicProvider translates DocumentBlock → {type: document, source: {type: text, ...}}
        └── model responds: ToolUseBlock(name="structured_output",
                            input={category: "complaint", confidence: 0.95, summary: "..."})
  └── output = tool_call.input = {category: "complaint", ...}
  └── write_targets → s3_main → documents-out/<run_id>.json
  └── decision record written: one model turn, one source, one target
```

No loop, no tools, no delegation, no HITL. One model call. The full engine
machinery handles it with the same code paths as the agent — just with a
`force_tool` and a `max_turns=1` quota.

---

## Layer map

| What just happened | Module covering it |
|---|---|
| CLI built the engine | 05 (factory / providers), 07 (connectors) |
| Sources resolved before the loop | 07 (binder source resolution) |
| First user message with DocumentBlock | 04 (_first_user_message), 02 (IR) |
| ModelChain picked a provider | 05 (chain, retry, fallback) |
| Provider translated IR → wire format | 05 (anthropic adapter) |
| Loop checked stop reason | 04 (stage 3) |
| Gateway auth check | 06 (step 1) |
| MCP tool dispatched | 06 (mcp transport), 07 (no connector involvement) |
| Python tool dispatched | 06 (python_inprocess) |
| Delegation ran a sub-agent | 06 (verity_builtin + _delegate), 04 (run_agent recursion) |
| HITL gate serialised continuation | 08 (gate + Continuation) |
| Resume rebuilt messages and continued | 08 (resume path) |
| Target written to S3 | 07 (write_targets) |
| Decision record assembled | 09 (assembler) |
| Two files written | 09 (FileSink) |
| Worker would have claimed/suspended/requeued | 10 (worker) |

---

## Where to go from here

**To understand the run's governance record:**
```bash
cat _artifacts/runs/$(date +%Y)/$(date +%m)/$(date +%d)/<run_id>/decision_log.json | python -m json.tool
```

**To reproduce the run deterministically:**
```bash
python -m harness.cli reproduce <run_id>
# replays recorded model turns; no API calls made
```

**To add a new tool to `underwriting_agent`:**
1. Register a Python function in `scripts/demo_app.py` or add an MCP tool to
   `mcp_servers/example_server.py`
2. Add `ToolAuthorization` to `packages/underwriting_agent.agent.yaml`
3. The engine, gateway, and decision log handle it automatically

**To add a new provider:**
1. Write a new adapter class in `harness/providers/`
2. Register it in `harness/core/factory.py`
3. Add it to a package's `inference.chain`

**To ship to production:**
- Swap `FileSink` for `PostgresSink` (`DECISION_SINK=postgres`)
- Start the worker (`python -m harness.cli worker`)
- Enqueue runs instead of calling `run_agent` directly
- Replace `FileContinuationStore` with a Postgres-backed store for HITL at scale
- The engine, loop, providers, tools, connectors, and decision assembler are
  unchanged — see `specs/INTEGRATION.md` for the full adoption guide

---

That completes the curriculum. Every piece of the harness has been covered:

```
Module 01 — Why the harness exists
Module 02 — The neutral IR: Messages, Blocks, ModelResponse
Module 03 — Package YAML: declaring agent behaviour
Module 04 — The execution engine: run_task, run_agent, _agent_loop, resume
Module 05 — Provider adapters and the fallback chain
Module 06 — Tool gateway: auth, mock seam, four transports, delegation
Module 07 — Connectors: Postgres, S3, binder, source/target resolution
Module 08 — HITL: Continuation, suspend, human decision, resume
Module 09 — Decision log: DecisionRecord, assembler, FileSink, PostgresSink
Module 10 — Worker: SKIP LOCKED, claim, execute, terminal states
Module 11 — End-to-end: all of the above in one run
```
