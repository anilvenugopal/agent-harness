# Module 08 — HITL: Human in the Loop

> Key file: `harness/hitl/continuation.py`
> Related: `harness/core/engine.py` (gate + resume), `harness/worker/worker.py`
> (suspend + requeue)
>
> This module covers the full suspend/resume lifecycle: what the engine
> serialises when it hits a gate, how a human records a decision, and how the
> run continues from where it stopped.

---

## The problem HITL solves

Some tool calls are irreversible. `bind_policy` issues a binder number to the
policy admin system. `send_wire_transfer` moves money. `deploy_to_production`
pushes code. These cannot be undone if the model got it wrong.

The naive solution — run the tool and let a human review the result after the
fact — gives humans no meaningful control over outcomes. The model has already
acted by the time the human looks.

HITL suspends the run *before* a gated tool executes, routes the pending request
to a reviewer, and resumes only after the human decides. The model proposed the
action; the human approved, modified, or denied it; the tool executes (or doesn't)
according to that decision.

The core constraint on implementation: **no worker, thread, or connection may
block waiting for the human**. A human might take hours or days to respond. A
worker that held a Postgres connection or an in-memory state object for that
duration would exhaust resources immediately in any realistic deployment.

The solution is to serialize the full run state to durable storage and raise an
exception. The worker catches the exception, marks the row `suspended`, and
releases. A suspended run costs only disk space — no compute, no memory.

The full lifecycle from initial run to post-approval completion:

```
  CLI / Worker                Engine Loop              Continuation Store
       │                           │                          │
       │── run_agent() ───────────►│                          │
       │                           │  turns 0–3 execute       │
       │                           │  (tools, delegation, ...) │
       │                           │                          │
       │                           │ turn 4: model requests   │
       │                           │ bind_policy              │
       │                           │                          │
       │                           │── save(Continuation) ───►│
       │                           │     (messages, turn,     │  continuations/
       │                           │      pending tool use)   │  {uuid}.json
       │                           │                          │
       │◄── HITLSuspended ─────────│                          │
       │    (suspension_id)        │                          │
       │                           │                          │
       │ mark run 'suspended'      │                          │
       │ WORKER RELEASED           │                          │
       │                           │                          │
       │  ···  human deliberates (minutes / hours)  ···       │
       │                           │                          │
       │── record_decision() ─────────────────────────────── ►│
       │   (approve/deny/edit)     │                          │  status →
       │                           │                          │  "resolved"
       │── resume(suspension_id) ──────────────────────────── │
       │                    load Continuation ◄───────────────│
       │                           │                          │
       │                           │ apply decision to        │
       │                           │ pending tool call        │
       │                           │                          │
       │                           │ re-enter _agent_loop     │
       │                           │ at turn 5                │
       │                           │                          │
       │                           │ turn 5: submit_output    │
       │                           │                          │
       │◄── ExecutionResult ───────│                          │
       │    status=complete        │                          │
```

The gap between "WORKER RELEASED" and "resume" can be arbitrarily long. No
resources are held during that time — only the JSON file on disk.

---

## What gets serialised: the `Continuation`

```python
class Continuation(BaseModel):
    id: UUID                      # the suspension ID
    run_id: UUID                  # the execution_run row
    decision_id: UUID             # the partial decision log record
    created_at: datetime

    # Run metadata — needed to rebuild the engine context on resume
    package_name: str
    package_version: str
    channel: str
    run_input: dict
    decision_depth: int

    # Loop state at the moment of suspension
    turn: int
    messages: list[dict]          # neutral Message dicts — the full conversation
    tool_calls_made: list[dict]   # tool records accumulated before the gate
    source_resolutions: list[dict]
    usage: dict

    # The pending tool call
    gate_tool: str
    pending_tool_use: dict        # {id, name, input}

    # Mock context (if the run was mocked)
    mock: Optional[dict]

    # Filled later
    status: str                   # "awaiting_decision" | "resolved"
    decision: Optional[HumanDecision]
```

The most critical field is `messages`. This is the complete conversation history
at the moment of suspension — every turn the model made, every tool result it
received — stored as neutral IR dicts. When `resume` loads this, it reconstructs
the exact conversation the model needs to continue reasoning, regardless of which
provider is called next.

`pending_tool_use` is the specific tool call that triggered the gate:
`{id: "toolu_01...", name: "bind_policy", input: {submission_id: 1, premium: 7438.18, ...}}`.

`tool_calls_made` contains the tool records for tools that *already ran* before
the gate (e.g. `rate_property` and `lookup_appetite` results). These are needed
to reconstruct the assembler state on resume so the final decision log correctly
shows everything that happened in the run.

---

## The gate in the engine loop

```python
# harness/core/engine.py — inside _agent_loop, Stage 5

for tc in response.tool_calls:
    if self.tools.needs_approval(tc.name, pkg.hitl):
        cont = Continuation(
            run_id=run_id,
            decision_id=assembler.record.id,
            package_name=pkg.name,
            package_version=pkg.version,
            channel=channel,
            run_input=run_input,
            decision_depth=depth,
            turn=turn,
            messages=[m.model_dump() for m in messages],
            tool_calls_made=list(assembler.record.tool_calls_made),
            source_resolutions=list(assembler.record.source_resolutions),
            usage=assembler.record.usage if hasattr(assembler.record, "usage") else {},
            gate_tool=tc.name,
            pending_tool_use={"id": tc.id, "name": tc.name, "input": tc.input},
            mock=mock.model_dump() if mock else None,
        )
        await self.continuations.save(cont)
        raise HITLSuspended(cont.id, run_id, tc.name)
```

`HITLSuspended` is a plain exception that carries the suspension ID. Everything
above the `raise` in the call stack that wants to catch it can. The engine does
not catch it — it propagates all the way to the worker (or the CLI).

If the model requested multiple tool calls in one turn (e.g. two tools in
parallel), the gate fires on the **first gated tool found**. Other pending tool
calls in the same turn are not executed.

---

## The three human decisions and what the model sees

```
Pending tool call:
  bind_policy(submission_id=1, premium=7438.18, limit=1850000, deductible=5000)

  ┌──────────────┐   ┌────────────────────────────────────────────────────────┐
  │   APPROVE    │──►│ bind_policy runs with original input                   │
  └──────────────┘   │ model receives: {binder_number: "BND-0001-...",        │
                     │                  status: "bound"}                      │
                     └────────────────────────────────────────────────────────┘

  ┌──────────────┐   ┌────────────────────────────────────────────────────────┐
  │     EDIT     │──►│ bind_policy runs with edited_input (corrected values)  │
  │ premium:7000 │   │     OR edited_result injected directly (skip the call) │
  └──────────────┘   │ model receives: the edited/injected result             │
                     └────────────────────────────────────────────────────────┘

  ┌──────────────┐   ┌────────────────────────────────────────────────────────┐
  │     DENY     │──►│ bind_policy NOT called                                 │
  └──────────────┘   │ model receives: ToolResultBlock(is_error=True,         │
                     │   content={"error": "Tool call denied..."})            │
                     │ model typically calls submit_output(decision="refer")  │
                     └────────────────────────────────────────────────────────┘
```

```python
class HumanDecision(BaseModel):
    decision: str                        # "approve" | "deny" | "edit"
    edited_input: Optional[dict] = None  # for "edit": run the tool with this input
    edited_result: Optional[Any] = None  # for "edit": skip the tool, inject this
    note: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: datetime = ...
```

| Decision | What resume does |
|---|---|
| `approve` | Runs the tool with the original `pending_tool_use.input` |
| `edit` | Runs the tool with `edited_input` instead, OR injects `edited_result` directly |
| `deny` | Injects `ToolResultBlock(is_error=True, content={"error": "Tool call denied"})` |

All three produce a `ToolResultBlock` that goes into the conversation. The model
sees the result of its proposed action and continues. With `approve`, it learns
what the tool actually did. With `deny`, it learns the action was refused and
must decide how to proceed (typically calls `submit_output` with `decision: refer`).

The `edit` path is the most powerful: a reviewer who spots a rate error in
`bind_policy`'s premium can correct the value and approve the corrected call.
The model resumes as if its original request succeeded with the corrected values.

---

## `FileContinuationStore`

```python
class FileContinuationStore:
    def __init__(self, root: str = "./_artifacts/suspensions"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: UUID) -> Path:
        return self.root / f"{sid}.json"

    async def save(self, cont: Continuation) -> UUID:
        self._path(cont.id).write_text(json.dumps(cont.model_dump(mode="json"), ...))
        return cont.id

    async def load(self, suspension_id: UUID) -> Continuation:
        return Continuation.model_validate_json(self._path(suspension_id).read_text())

    async def record_decision(self, suspension_id: UUID, decision: HumanDecision) -> Continuation:
        cont = await self.load(suspension_id)
        cont.decision = decision
        cont.status = "resolved"
        self._path(suspension_id).write_text(json.dumps(cont.model_dump(mode="json"), ...))
        return cont

    async def list_pending(self) -> list[Continuation]:
        return [Continuation.model_validate_json(p.read_text())
                for p in self.root.glob("*.json")
                if json.loads(p.read_text()).get("status") == "awaiting_decision"]
```

Each continuation is a single JSON file named `{suspension_id}.json`. The file
is written atomically (the entire file is re-serialised on every update). In
production, this store would be backed by a Postgres table (the `suspension_id`
column on `execution_run` is the hook), but the file store is sufficient for
the demo and has identical protocol semantics.

---

## Resume: rebuilding from the continuation

```python
# harness/core/engine.py — resume()

async def resume(self, suspension_id: UUID) -> ExecutionResult:
    cont = await self.continuations.load(suspension_id)
    pkg = self._require_package(cont.package_name, ...)

    # Rebuild the message list from neutral IR dicts.
    messages = [Message.model_validate(m) for m in cont.messages]

    # Restore the assembler from what was accumulated before the gate.
    assembler = self._new_assembler(pkg, cont.run_id, ...)
    for tc in cont.tool_calls_made:
        assembler.add_tool_call(tc)
    for sr in cont.source_resolutions:
        assembler.add_source_resolution(sr)

    # Apply the human decision to the pending tool call.
    decision = cont.decision
    pending = cont.pending_tool_use
    authorized = pkg.tools

    if decision.decision == "approve":
        rec = await self.tools.call(name=pending["name"], tool_input=pending["input"],
                                    authorized=authorized, ...)
    elif decision.decision == "edit" and decision.edited_result is not None:
        rec = _ok(pending["name"], pending["input"], 0, decision.edited_result, ...)
    elif decision.decision == "edit":
        rec = await self.tools.call(name=pending["name"],
                                    tool_input=decision.edited_input, ...)
    else:  # deny
        rec = {"tool_name": pending["name"], "error": True,
               "output_data": {"error": "Tool call denied by reviewer. Note: " + (decision.note or "")}}

    assembler.add_tool_call(rec)
    messages.append(Message.tool_results([ToolResultBlock(
        tool_use_id=pending["id"],
        content=rec["output_data"],
        is_error=rec["error"],
    )]))

    # Re-enter the loop at turn+1.
    quota = QuotaEnforcer(max_turns=pkg.inference.max_turns, ...)
    return await self._agent_loop(
        pkg=pkg, ..., messages=messages, assembler=assembler,
        quota=quota, start_turn=cont.turn + 1, ...
    )
```

`Message.model_validate(m)` reconstructs typed IR objects from the JSON dicts.
The resulting `messages` list is identical in structure to what the loop would
have had if it hadn't suspended. The loop continues at `turn + 1` as if nothing
had interrupted it.

---

## The worker's HITL path

```python
# harness/worker/worker.py — _execute

async def _execute(self, run: dict) -> None:
    try:
        result = await self.engine.run_agent(agent_name=name, context=inp, ...)
        await self._mark_terminal(run_id, result)
    except HITLSuspended as s:
        await self._mark_suspended(run_id, s.suspension_id)
    except Exception as e:
        await self._mark_failed(run_id, ...)
```

On `HITLSuspended`:

```python
async def _mark_suspended(self, run_id, suspension_id) -> None:
    async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
        await conn.execute(
            "UPDATE execution_run SET status='suspended', suspension_id=%(sid)s WHERE id=%(id)s",
            {"sid": str(suspension_id), "id": str(run_id)},
        )
        await conn.commit()
    # Worker is now completely free. No state held in memory.
```

After this returns, the worker immediately polls for the next queued run. The
suspended run is just a row with `status='suspended'` and a `suspension_id`.

When a human records a decision (via the CLI's `resume` command or any other
path), `requeue_resumed` flips the row back to `queued`:

```python
async def requeue_resumed(dsn: str, run_id: UUID) -> None:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            "UPDATE execution_run SET status='queued' WHERE id=%(id)s AND status='suspended'",
            {"id": str(run_id)},
        )
        await conn.commit()
```

A worker's next `claim_one` call picks it up, claims it with `status='executing'`,
calls `engine.resume(continuation_id)`, and continues the run. The worker that
resumes a run may be a different process or machine from the one that started it —
all state is in the continuation file and the Postgres row.

---

## The demo's `--auto-approve` path

In the demo, HITL suspension is handled in the CLI itself:

```python
# harness/cli.py — cmd_demo

try:
    res = await engine.run_agent(...)
except HITLSuspended as s:
    print(f"⏸  SUSPENDED: gate={s.gate_tool!r} suspension={s.suspension_id}")
    if args.auto_approve:
        await engine.continuations.record_decision(
            s.suspension_id,
            HumanDecision(decision="approve", decided_by="demo")
        )
        res = await engine.resume(s.suspension_id)
```

`--auto-approve` simulates an instant approval in the same process. Without it,
the CLI prints the suspension ID and exits; you resume with:

```bash
python -m harness.cli resume <suspension_id> --decision approve
python -m harness.cli resume <suspension_id> --decision deny --note "TIV too high"
python -m harness.cli resume <suspension_id> --decision edit \
    --edited-input '{"submission_id": 1, "premium": 7000.00, "limit": 1850000, "deductible": 5000}'
```

---

## Scope constraint: top-level agents only

HITL gates only fire when `depth == 0`. A sub-agent at depth 1 whose package
declares a HITL policy on a tool does not suspend — the tool is denied with an
error:

```
Tool call denied: HITL gates cannot fire inside a delegated sub-agent.
A synchronous delegation blocks the parent; top-level HITL only.
```

Why? If the sub-agent suspended, the parent loop's `delegate_to_agent` call
would block indefinitely — it is waiting for the sub-agent to return a result.
There is no mechanism to unblock it while the human deliberates. Async delegation
(where the parent loop gets a "child started" acknowledgement and continues) would
solve this, but it is out of scope for this build.

---

## Neutral IR storage — why not vendor format

The `messages` field in `Continuation` stores neutral IR dicts (pydantic models
serialised with `model.model_dump()`). It does NOT store Anthropic message
params, OpenAI input items, or Gemini content objects.

This matters for two reasons:

1. **Provider agnosticism on resume.** The run that suspended might have used
   Anthropic. The run that resumes might use OpenAI (if Anthropic's API is down
   and fallback is enabled). The neutral format is translated to whatever
   provider is active at resume time.

2. **Correct model of conversation.** The neutral IR is what the engine *actually
   works with*. Storing vendor format would require each provider to also implement
   a "deserialise your own format" path, which adds coupling and failure modes.

---

## Checkpoint

1. The engine suspends mid-run. The `Continuation.messages` field contains 8
   message dicts (4 turns × 2 messages each). When `resume` loads these, what
   Python type does each dict become?

2. The human chooses `decision: edit` and provides `edited_result: {"binder_number": "BND-MANUAL", "status": "bound"}`.
   Is `bind_policy` actually called? What does the model receive?

3. A sub-agent at depth 1 tries to execute `bind_policy`, which is in its HITL
   policy. Why doesn't the gate fire? What does the model receive instead?

4. After `HITLSuspended` is raised, is any state held in the worker's memory?

5. Two workers are running. A suspended run is re-queued. Can both workers claim
   it? What prevents that?

6. Why is the continuation stored as neutral IR dicts rather than Anthropic API
   params?

When you can answer these, move to **[Module 09: The Decision Log](09-decision-log.md)**
— what the governance record contains, how it's assembled turn-by-turn, and how
the two sinks (file and Postgres) serve different phases of the adoption lifecycle.
