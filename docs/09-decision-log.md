# Module 09 — The Decision Log: Governance Record

> Key file: `harness/decisions/assembler.py`
> Related: `harness/core/engine.py` (assembler wiring), `docker/initdb/01_schema.sql`
>
> This module covers the `DecisionRecord` schema, how the `DecisionAssembler`
> accumulates it throughout a run, and the two sink implementations that persist it.

---

## Why a decision log exists

Every model run produces outputs with real-world consequences: an underwriting
decision, a classified document, a bound policy. Governance requires answers to:

- What input did the agent see?
- What did each model turn produce?
- Which tools were called, with what arguments, returning what results?
- Where did data come from (which sources), and where did it go (which targets)?
- How much did it cost? How long did it take?
- Was a human involved?
- Can we reproduce this run exactly?

The decision log is the single record that answers all of these. It is written
after every run — success, failure, HITL suspension, or quota exceeded. It is
never lost mid-run because the assembler accumulates state incrementally and the
sink writes atomically at the end.

---

## The `DecisionRecord` fields

```python
class DecisionRecord(BaseModel):
    # Identity
    id: UUID                          # the decision log's own UUID
    entity_kind: str                  # "task" | "agent"
    entity_name: str                  # "underwriting_agent"
    entity_version: str               # "v1"
    channel: str                      # "production" | "staging" | ...
    mock_mode: bool                   # true if any gateway was mocked

    # Correlation
    workflow_run_id: Optional[UUID]   # ties runs in a workflow together
    execution_run_id: Optional[UUID]  # the execution_run row (worker mode)
    parent_decision_id: Optional[UUID]  # links sub-agent to parent
    decision_depth: int               # 0 = top-level, 1 = first sub-agent
    step_name: Optional[str]
    reproduced_from_decision_id: Optional[UUID]  # set when this is a replay

    # I/O
    input_json: dict
    output_json: dict
    reasoning_text: Optional[str]     # model's final free-text if no submit_output

    # Model / cost
    model_chain: list[dict]           # the configured chain (not just what ran)
    models_used: list[str]            # models that actually responded
    input_tokens: int                 # total across all turns
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    duration_ms: int

    # Nested audit
    message_history: list[dict]       # full conversation in neutral IR
    tool_calls_made: list[dict]       # every tool: input, output, transport
    source_resolutions: list[dict]    # every source: connector, value_preview
    target_writes: list[dict]         # every target: container, handle, suppressed?

    # Governance
    application: str                  # "harness"
    status: str                       # complete | failed | suspended | max_turns
    error_message: Optional[str]
    hitl_required: bool
    created_at: datetime
```

`message_history` stores the full conversation in neutral IR — the same format as
`Continuation.messages`. This means a decision log can be loaded and the
conversation replayed without any provider-specific parsing. It is also what the
`reproduce` command uses.

`tool_calls_made` stores every gateway record verbatim — `tool_name`, `input_data`,
`output_data`, `transport`, `error`, `mock_mode`, `mock_source`. This is the full
audit trail of effects.

---

## How the record grows over a run's lifetime

```
RUN START ──────────────────────────────────────────────────────────────────►
│
│  _new_assembler()
│    record pre-populated: id, entity_kind, entity_name, input_json,
│                          model_chain (configured), hitl_required
│
│  binder.resolve_sources()
│    assembler.add_source_resolution(src_audit)  ← one per source
│      record.source_resolutions grows
│
│  ┌─ LOOP TURN 0 ──────────────────────────────────────────────────────┐
│  │  model call → response                                             │
│  │  assembler.add_turn(turn=0, response)                              │
│  │    record.input_tokens  += response.usage.input_tokens             │
│  │    record.output_tokens += response.usage.output_tokens            │
│  │    record.models_used   += [response.model]  (if new)              │
│  │    model_invocations[0] = {turn, provider, model, blocks, usage}   │
│  │                                                                    │
│  │  gateway.call() → tool_record                                      │
│  │  assembler.add_tool_call(tool_record)                              │
│  │    record.tool_calls_made grows                                    │
│  └────────────────────────────────────────────────────────────────────┘
│
│  ┌─ LOOP TURN 1 ──────────────────────────────────────────────────────┐
│  │  model call → response                                             │
│  │  assembler.add_turn(turn=1, response)   ← tokens accumulate again  │
│  │  assembler.add_tool_call(...)           ← another record appended  │
│  └────────────────────────────────────────────────────────────────────┘
│
│  … (N more turns) …
│
│  submit_output → loop exits
│  assembler.set_messages(messages)
│    record.message_history = [all N messages serialised]
│
│  binder.write_targets()
│    assembler.add_target_write(tgt_audit)
│
│  assembler.finish(output, status, duration_ms)
│    record.output_json, status, duration_ms set
│
│  sink.write(assembler)
│    decision_log.json  ← full record
│    model_invocations.jsonl  ← one line per turn
│
RUN END ──────────────────────────────────────────────────────────────────►
```

Token counts accumulate at each `add_turn` call — not summed at the end. A run
that exits early (quota exceeded after turn 2) still has accurate token totals
for the turns that did execute.

---

## The assembler — accumulating throughout the run

The `DecisionAssembler` is constructed at the start of each run and handed to
every stage that produces auditable data:

```python
# harness/core/engine.py — at start of run_task / run_agent
assembler = self._new_assembler(pkg, run_id, ...)
```

```python
def _new_assembler(self, pkg, run_id, ...) -> DecisionAssembler:
    rec = DecisionRecord(
        id=run_id,
        entity_kind=pkg.kind.value,
        entity_name=pkg.name,
        entity_version=pkg.version,
        input_json=input_data,
        model_chain=[ref.model_dump() for ref in pkg.inference.chain],
        hitl_required=bool(pkg.hitl and pkg.hitl.enabled),
        ...
    )
    return DecisionAssembler(rec)
```

The record is pre-populated with everything known at startup: identity, input,
model chain configuration, HITL flag. Everything that depends on execution
(output, tool calls, tokens) is added incrementally.

### `add_turn` — after each model response

```python
def add_turn(self, *, turn: int, response, neutral_blocks: list[dict]) -> None:
    u: Usage = response.usage
    self.record.input_tokens  += u.input_tokens
    self.record.output_tokens += u.output_tokens
    self.record.cache_read_tokens  += u.cache_read_tokens
    self.record.cache_write_tokens += u.cache_write_tokens
    if response.model not in self.record.models_used:
        self.record.models_used.append(response.model)
    self.model_invocations.append({
        "turn":        turn,
        "provider":    response.provider,
        "model":       response.model,
        "request_id":  response.request_id,
        "stop_reason": response.stop_reason.value,
        "usage":       u.model_dump(),
        "blocks":      neutral_blocks,    # IR blocks in dict form
    })
```

`model_invocations` is a separate list that becomes `model_invocations.jsonl` —
one JSON line per turn. The `blocks` field contains the exact blocks the model
returned (text, tool_use, thinking), in neutral form. This is the data the
`reproduce` command replays.

Token counts are accumulated here — not summed at the end. This means the record
has accurate totals even if the loop exits early (quota exceeded, error).

### `add_tool_call` / `add_source_resolution` / `add_target_write`

These are simple list appends. The gateway, binder, and engine call them as each
event occurs:

```python
rec = await self.tools.call(...)
assembler.add_tool_call(rec)              # immediately after dispatch

ctx, src_audit = await self.binder.resolve_sources(...)
for a in src_audit:
    assembler.add_source_resolution(a)   # immediately after resolution

tgt_audit = await self.binder.write_targets(...)
for a in tgt_audit:
    assembler.add_target_write(a)        # immediately after writes
```

### `set_messages` and `finish`

After the loop exits:

```python
assembler.set_messages(messages)   # full conversation → message_history
return await self._finish(assembler, output, reasoning, status, error, duration_ms)
```

```python
def finish(self, *, output, reasoning, status, error, duration_ms) -> DecisionRecord:
    self.record.output_json    = output
    self.record.reasoning_text = reasoning
    self.record.status         = status
    self.record.error_message  = error
    self.record.duration_ms    = duration_ms
    return self.record
```

`finish` seals the record. After this, the sink writes it.

---

## The two sinks

### `FileSink` — the default, zero-infrastructure sink

```
_artifacts/
└── runs/
    └── 2026/
        └── 06/
            └── 15/
                ├── a2c1f3e7-.../           ← underwriting_agent run (depth=0)
                │   ├── decision_log.json   ← full DecisionRecord (all fields)
                │   └── model_invocations.jsonl
                │         line 1: {"turn":0,"provider":"anthropic","model":"claude-opus-4-8","blocks":[...],...}
                │         line 2: {"turn":1,"provider":"anthropic","blocks":[...],...}
                │         line 3: ...
                │
                └── 7f3ab8c2-.../           ← loss_history_analyst run (depth=1)
                    ├── decision_log.json   ← parent_decision_id: "a2c1f3e7-..."
                    └── model_invocations.jsonl
                          line 1: {"turn":0,...}
                          line 2: {"turn":1,...}
```

The `reproduce` command searches for the right directory with `rglob(f"{run_id}/model_invocations.jsonl")`,
so you can pass just the run UUID without knowing the date path.



```python
class FileSink:
    async def write(self, assembler: DecisionAssembler) -> UUID:
        rec = assembler.record
        run_dir = self.root / "runs" / f"{rec.created_at:%Y}" / f"{rec.created_at:%m}" \
                             / f"{rec.created_at:%d}" / str(rec.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "decision_log.json").write_text(
            json.dumps(rec.model_dump(mode="json"), indent=2, default=str)
        )
        with (run_dir / "model_invocations.jsonl").open("w") as f:
            for inv in assembler.model_invocations:
                f.write(json.dumps(inv, default=str) + "\n")
        return rec.id
```

The layout is ADR-0015's per-run artifact structure:

```
_artifacts/runs/2026/06/15/<run_id>/
    decision_log.json          ← DecisionRecord (all 31 fields)
    model_invocations.jsonl    ← one JSON object per model turn
```

The date-partitioned directory structure allows efficient listing and archival
without indexing. A governance hub that ingests these files can walk the tree
by date range.

**`model_invocations.jsonl` enables bit-exact reproduction.** The file contains
the neutral IR blocks returned by the model on each turn. The `reproduce` command
loads them and feeds them to a `MockProvider(replay=recorded)`, which returns
them in order. The engine runs the same logic with the same model "responses"
and produces the same outputs.

**Why two files instead of one?** The decision log is the governance record
humans and systems query — it has a stable schema, flat scalars, and nested JSON
for audit detail. The model invocations are the replay record — potentially large
(many turns, long text blocks) and only needed for the reproduction path. Keeping
them separate avoids bloating the governance record with replay data.

### `PostgresSink` — the production sink

```python
class PostgresSink:
    async def write(self, assembler: DecisionAssembler) -> UUID:
        import psycopg
        rec = assembler.record
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(
                """INSERT INTO agent_decision_log
                   (id, entity_kind, entity_name, ..., message_history, tool_calls_made,
                    source_resolutions, target_writes, ...)
                   VALUES (%(id)s, ..., %(message_history)s, ...)""",
                {
                    "id": str(rec.id),
                    "message_history": json.dumps(rec.message_history, default=str),
                    "tool_calls_made": json.dumps(rec.tool_calls_made, default=str),
                    ...
                }
            )
            await conn.commit()
        return rec.id
```

The `agent_decision_log` table (created by `docker/initdb/01_schema.sql`) mirrors
the `DecisionRecord` exactly — flat scalars for queryable fields, `JSONB` columns
for nested audit data. The `JSONB` columns are queryable by Postgres operators
(`->>`, `@>`), so you can write SQL like:

```sql
SELECT id, entity_name, input_json, output_json
FROM agent_decision_log
WHERE tool_calls_made @> '[{"tool_name": "bind_policy"}]'
  AND created_at > now() - interval '7 days';
```

**Switching sinks is one line in the factory:**

```python
# harness/core/factory.py
sink: DecisionSink = (
    PostgresSink(os.environ["DECISION_PG_DSN"])
    if os.environ.get("DECISION_SINK") == "postgres"
    else FileSink(artifacts_root)
)
```

Both sinks implement `DecisionSink`:

```python
class DecisionSink(Protocol):
    async def write(self, assembler: DecisionAssembler) -> UUID: ...
```

The engine calls `await self.sink.write(assembler)` and does not know which
implementation it's talking to.

---

## Decision records in a delegation tree

When `underwriting_agent` delegates to `loss_history_analyst`, two separate
decision records are written:

```
loss_history_analyst (depth=1)
  id: 7f3a...
  entity_name: loss_history_analyst
  decision_depth: 1
  parent_decision_id: a2c1...    ← links to parent
  status: complete
  output: {loss_count: 1, loss_ratio: 0.15, ...}

underwriting_agent (depth=0)
  id: a2c1...
  entity_name: underwriting_agent
  decision_depth: 0
  parent_decision_id: null
  status: complete
  output: {decision: "bound", premium: 7438.18, ...}
  tool_calls_made: [
    {..., tool_name: "delegate_to_agent",
     output_data: {sub_decision_log_id: "7f3a...", sub_status: "complete", output: {...}}}
  ]
```

The child record is written first (when `run_agent` returns for the child), then
the parent record is written (when the parent loop exits). The `parent_decision_id`
field links them. A governance database can reconstruct the full delegation tree
with a recursive CTE:

```sql
WITH RECURSIVE tree AS (
    SELECT * FROM agent_decision_log WHERE id = 'a2c1...'
    UNION ALL
    SELECT d.* FROM agent_decision_log d
    JOIN tree t ON d.parent_decision_id = t.id
)
SELECT id, entity_name, decision_depth, status, output_json FROM tree;
```

---

## The reproduction path

```python
# harness/cli.py — cmd_reproduce

sink = FileSink(ARTIFACTS)
recorded = sink.load_model_invocations(run_id)   # reads model_invocations.jsonl

shared_replay = MockProvider(replay=recorded)
providers = {name: shared_replay for name in ("anthropic", "openai", "gemini", "mock")}
mock = MockContext(model_replay=recorded, mock_all_tools=True, suppress_targets=True)

res = await engine.run_agent(agent_name=..., context=dec["input_json"], mock=mock)
```

`load_model_invocations` walks the artifact tree to find the `.jsonl` file:

```python
def load_model_invocations(self, run_id: str | UUID) -> list[dict]:
    for path in self.root.rglob(f"{run_id}/model_invocations.jsonl"):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    raise FileNotFoundError(...)
```

The reproduced run writes a **new** decision record (`reproduced_from_decision_id`
links it to the original). It does not overwrite the original. The new record's
`mock_mode: true` flag marks it as a reproduction, not a live run.

---

## Checkpoint

1. A run has 5 model turns. How many lines does `model_invocations.jsonl` have?
   What does each line contain?

2. The `message_history` in the decision record contains `ToolUseBlock` and
   `ToolResultBlock` entries in neutral IR form. Why is this more useful for the
   governance record than storing the Anthropic API response objects?

3. `model_chain` and `models_used` are different fields. What is the difference?
   Give an example where they differ.

4. A run fails because an MCP server is unreachable. Is a decision record still
   written? What fields change compared to a successful run?

5. Switching from `FileSink` to `PostgresSink` requires changing one line in the
   factory. Why doesn't the engine itself need to change?

6. The `reproduce` command sets `mock_all_tools=True`. Why is this necessary for
   bit-exact reproduction? What would go wrong if tools ran live?

When you can answer these, move to **[Module 10: The Worker](10-worker.md)** —
the Postgres SKIP LOCKED claim loop, graceful shutdown, and the enqueue/resume
helpers.
