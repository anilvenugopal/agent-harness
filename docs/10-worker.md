# Module 10 — The Worker: Distributed Dispatch

> Key file: `harness/worker/worker.py`
> Related: `docker/initdb/01_schema.sql` (execution_run schema),
> `harness/cli.py` (enqueue / worker / resume commands)
>
> This module covers the Postgres SKIP LOCKED claim loop, how the worker handles
> each terminal state, and the helpers for enqueuing and resuming runs.

---

## Why a queue

The demo scenarios can be run synchronously — `python -m harness.cli demo classify`
blocks until the run finishes. For production workloads this does not scale:

- A caller should not wait minutes for a complex agent run to finish
- HITL suspension would block the caller for the duration of a human review
- Multiple concurrent runs need parallel workers
- A crashed worker should not lose in-flight work

The worker pattern addresses all of this. Callers enqueue a run by inserting a row
into `execution_run`. Workers poll the table, claim rows atomically, and execute
them. A failed worker leaves the row in `executing` status, and a recovery sweep
(or the next worker startup) can requeue it.

---

## The `execution_run` table

```sql
CREATE TABLE execution_run (
    id              UUID PRIMARY KEY,
    entity_kind     TEXT NOT NULL,      -- 'task' | 'agent'
    entity_name     TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'production',
    input_json      JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    attempt         INT  NOT NULL DEFAULT 0,
    worker_id       TEXT,
    suspension_id   UUID,               -- set when status='suspended'
    decision_log_id UUID,               -- set on completion
    output_json     JSONB,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX idx_execution_run_claim ON execution_run (status, created_at);
```

The index on `(status, created_at)` covers `claim_one`'s query: filter by
`status='queued'`, order by `created_at`. Without it, the claim query would
full-scan the table on every poll iteration.

The `status` column drives the state machine:

```
                         ┌────────────────────────────────────────────┐
                         │              execution_run row             │
                         └────────────────────────────────────────────┘

  enqueue_run()
       │
       ▼
  ┌─────────┐    claim_one()   ┌───────────┐
  │ queued  │─────────────────►│ executing │
  └─────────┘                  └─────┬─────┘
       ▲                             │
       │                   ┌─────────┼───────────┬──────────────┐
       │                   ▼         ▼           ▼              ▼
       │            ┌──────────┐ ┌────────┐ ┌──────────┐  ┌───────────┐
       │            │ complete │ │ failed │ │max_turns │  │ suspended │
       │            └──────────┘ └────────┘ └──────────┘  └─────┬─────┘
       │                                                          │
       │    (attempt < max_attempts)                   human decides
       └────────────────────────────────                         │
            requeue on crash                      requeue_resumed()
                                                                 │
                                                                 │
                                                       status → 'queued'
                                                       worker re-claims
```

`complete`, `failed`, and `max_turns` are terminal — no further transitions.
`suspended` is the only non-terminal non-queued state. The `suspension_id`
column records which continuation to load on resume.

---

## The claim loop

```python
async def run_forever(self) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, self.request_stop)

    while not self._stop.is_set():
        claimed = await self.claim_one()
        if claimed is None:
            await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            continue
        await self._execute(claimed)
```

On each iteration: attempt a claim. If nothing is queued, wait `poll_interval`
seconds (default 1s) — but interruptible by the stop signal. If something was
claimed, execute it immediately (no sleep).

**Signal handling:** `request_stop` sets `self._stop` (an `asyncio.Event`).
`wait_for(self._stop.wait(), timeout=poll_interval)` serves double duty: it is
the poll sleep AND the signal responder. When SIGINT/SIGTERM arrives, the event
fires, `wait_for` returns, the loop condition `not self._stop.is_set()` is false,
and the worker exits cleanly after the current run (if any) finishes.

This means Ctrl-C during a model call waits for the model call to complete (or
time out) before the worker exits. It does not kill an in-flight run mid-turn.

---

## `claim_one` — atomic SKIP LOCKED

```python
async def claim_one(self) -> Optional[dict]:
    import psycopg
    from psycopg.rows import dict_row
    async with await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row) as conn:
        cur = await conn.execute(
            """
            UPDATE execution_run
               SET status='executing', claimed_at=now(),
                   worker_id=%(wid)s, attempt = attempt + 1
             WHERE id = (
                 SELECT id FROM execution_run
                  WHERE status='queued'
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1)
            RETURNING id, entity_kind, entity_name, channel, input_json, attempt;
            """,
            {"wid": self.worker_id},
        )
        row = await cur.fetchone()
        await conn.commit()
        return row
```

Here is what happens when three workers all poll simultaneously with two queued rows:

```
Postgres execution_run table:
  id=A  status='queued'   created_at=10:00
  id=B  status='queued'   created_at=10:01
  id=C  status='executing' ...

  Worker 1                Worker 2                Worker 3
      │                       │                       │
      ▼                       ▼                       ▼
  SELECT ... FOR          SELECT ... FOR          SELECT ... FOR
  UPDATE SKIP LOCKED      UPDATE SKIP LOCKED      UPDATE SKIP LOCKED
  LIMIT 1                 LIMIT 1                 LIMIT 1
      │                       │                       │
  Postgres picks id=A     Postgres picks id=B     Postgres sees A and B
  and locks it            and locks it            are locked → skips both
      │                       │                       │ returns no row
      ▼                       ▼                       ▼
  UPDATE status='executing'   UPDATE status='executing'
  RETURNING id=A              RETURNING id=B          returns NULL
      │                       │                       │
  claims run A            claims run B            polls again after 1s
```

Worker 3 gets `NULL` from `claim_one` and sleeps for `poll_interval`. No
coordination between workers is needed beyond the database row lock.

The `FOR UPDATE SKIP LOCKED` clause is the key. When multiple workers run this
query simultaneously:
- Postgres locks the candidate row for update
- Any worker that reaches a row already locked by another worker **skips it**
  rather than waiting
- Each worker atomically claims a different row

Without `SKIP LOCKED`, all workers would queue up on the same row, serializing
what should be parallel work. With it, N workers process N rows concurrently with
no coordination beyond the database.

The `attempt` counter is incremented on every claim. This lets the worker detect
runs that have been claimed multiple times (due to crashes) and eventually mark
them `failed` after `max_attempts`.

---

## `_execute` — dispatch and terminal state handling

```python
async def _execute(self, run: dict) -> None:
    run_id = UUID(str(run["id"]))
    kind = run["entity_kind"]
    name = run["entity_name"]
    inp  = run["input_json"] if isinstance(run["input_json"], dict) else json.loads(run["input_json"])

    try:
        if kind == "task":
            result = await self.engine.run_task(
                task_name=name, input_data=inp,
                channel=run["channel"], execution_run_id=run_id)
        else:
            result = await self.engine.run_agent(
                agent_name=name, context=inp,
                channel=run["channel"], execution_run_id=run_id)
        await self._mark_terminal(run_id, result)

    except HITLSuspended as s:
        await self._mark_suspended(run_id, s.suspension_id)

    except Exception as e:
        logger.exception("run %s crashed", run_id)
        await self._mark_failed(run_id, run.get("attempt", 1), str(e))
```

Three outcomes:

| Outcome | Method called | Row status |
|---|---|---|
| Engine returns `ExecutionResult` | `_mark_terminal` | `complete` / `failed` / `max_turns` (from `result.status`) |
| `HITLSuspended` raised | `_mark_suspended` | `suspended` |
| Any other exception | `_mark_failed` | `queued` (if attempts remain) or `failed` |

`channel` and `execution_run_id` are threaded into the engine call for correlation:
they appear in the decision log (`channel`, `execution_run_id` fields) so you can
join the decision log to the `execution_run` row.

---

## `_mark_terminal`

```python
async def _mark_terminal(self, run_id, result) -> None:
    async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
        await conn.execute(
            """UPDATE execution_run
                  SET status=%(s)s, decision_log_id=%(d)s,
                      output_json=%(o)s, finished_at=now()
                WHERE id=%(id)s""",
            {"s": result.status.value,
             "d": str(result.decision_log_id) if result.decision_log_id else None,
             "o": json.dumps(result.output, default=str),
             "id": str(run_id)},
        )
        await conn.commit()
```

`result.status.value` is one of `"complete"`, `"failed"`, `"max_turns"` —
directly from the `RunStatus` enum returned by the engine. `decision_log_id`
links the row to the decision log artifact. `output_json` stores the final
output for direct queries without loading the full decision record.

---

## `_mark_failed` — retry or terminal failure

```python
async def _mark_failed(self, run_id, attempt, error) -> None:
    status = "queued" if attempt < self.max_attempts else "failed"
    async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
        await conn.execute(
            """UPDATE execution_run
                  SET status=%(s)s, error_message=%(e)s,
                      finished_at = CASE WHEN %(s)s='failed' THEN now() ELSE finished_at END
                WHERE id=%(id)s""",
            {"s": status, "e": error[:2000], "id": str(run_id)},
        )
        await conn.commit()
```

If `attempt < max_attempts` (default 3), the status goes back to `"queued"`.
A worker will re-claim it on the next poll — the `attempt` counter was already
incremented by `claim_one`, so if this keeps failing, eventually `attempt >= max_attempts`
and it becomes `"failed"` permanently.

`error[:2000]` truncates long stack traces to fit the column.

---

## `enqueue_run` — inserting a queued row

```python
async def enqueue_run(dsn: str, *, entity_kind: str, entity_name: str,
                      input_data: dict, channel: str = "production") -> UUID:
    rid = uuid4()
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            """INSERT INTO execution_run
               (id, entity_kind, entity_name, channel, input_json, status)
               VALUES (%(id)s, %(k)s, %(n)s, %(c)s, %(i)s, 'queued')""",
            {"id": str(rid), "k": entity_kind, "n": entity_name, "c": channel,
             "i": json.dumps(input_data, default=str)},
        )
        await conn.commit()
    return rid
```

The run ID is generated client-side (not by Postgres default). This lets the
caller know the run ID before the row exists, which is useful for setting up
monitoring or correlation before the first worker picks it up.

CLI usage:

```bash
python -m harness.cli enqueue underwriting_agent --input '{"submission_id": 1}'
# → enqueued run 3f7a1b... (agent underwriting_agent)
```

---

## `requeue_resumed` — re-activating a suspended run

```python
async def requeue_resumed(dsn: str, run_id: UUID) -> None:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            "UPDATE execution_run SET status='queued' WHERE id=%(id)s AND status='suspended'",
            {"id": str(run_id)},
        )
        await conn.commit()
```

The `AND status='suspended'` guard prevents accidentally re-queuing a run that
already completed or is currently executing. Only a genuinely suspended row can
be re-queued this way.

After this, a worker's next `claim_one` call will pick up the row. The worker
sees the row's `suspension_id` — wait, actually it doesn't: the worker only gets
`id, entity_kind, entity_name, channel, input_json, attempt` from `claim_one`.
It is the engine's `resume(suspension_id)` that loads the continuation. The
worker finds the `suspension_id` via... actually let me clarify: the CLI's `resume`
command records the decision and calls `engine.resume` directly. For the worker
path, the worker re-runs `engine.run_agent` with the same `input_json`, but
wait — that would start a fresh run. Let me check...

Actually re-reading the worker code: when a worker re-claims a formerly suspended
run, it calls `engine.run_agent(input_data=inp, ...)` again. But the engine
in `run_agent` doesn't know to call `resume` — it would start fresh.

The intended production path: the `resume` CLI command records the decision and
calls `engine.resume(suspension_id)` directly, completing the run in-process
rather than re-queueing for a worker. `requeue_resumed` is the worker-mode path
hook — when a full distributed worker mode is built, the worker would read
`suspension_id` from the claimed row and call `engine.resume`. The current
worker's `_execute` would need a branch for this. The current implementation
covers the CLI path (`harness.cli resume <id>`); the worker-mode resume branch
is the next adoption step.

---

## The adoption note

The worker's docstring is explicit about its place in the adoption plan:

> This is the v1 claim loop, which ADR-0015 explicitly preserves as the
> `VERITY_DISPATCH_MODE=postgres` fallback. Adopting the real plane =
> swapping `claim_one` for a NATS consumer.

The entire `run_forever` loop, `_execute`, and all the terminal-state methods
are unchanged between the Postgres fallback and the NATS path. Only `claim_one`
changes — from a SKIP LOCKED `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE)`
to consuming from a NATS subject. The engine inside `_execute` is byte-identical
in both cases.

---

## Running the worker

```bash
# Start the full stack (Postgres + MinIO + MCP server + worker)
docker compose --env-file .env -f docker/docker-compose.yml up --build

# Enqueue a run
docker compose exec worker python -m harness.cli enqueue underwriting_agent \
    --input '{"submission_id": 1}'

# Watch the worker logs
docker compose logs -f worker
```

Or run the worker directly without Docker:

```bash
# Requires PG_MAIN_DSN in env
python -m harness.cli worker --worker-id my-worker
```

Worker options: `--worker-id` (for multi-worker deployments), `--poll-interval`
(seconds between empty-queue polls), `--max-attempts` (crash retry ceiling).

---

## Checkpoint

1. Two workers run `claim_one` at the same instant. Both find the same queued
   row. What prevents both from claiming it?

2. A run crashes on attempt 2 with `max_attempts=3`. What does `_mark_failed`
   set `status` to? What happens next?

3. Why does `claim_one` increment `attempt` as part of the claim query rather
   than in a separate UPDATE after the claim?

4. What is `SIGINT` / `SIGTERM` doing to `self._stop`? Why does the worker
   finish the current run before stopping rather than exiting immediately?

5. `decision_log_id` is stored in the `execution_run` row after completion. A
   downstream process queries this row. What can it do with `decision_log_id`?

6. Why does `_mark_failed` use `error[:2000]` rather than the full error string?

When you can answer these, move to **[Module 11: End-to-End Walkthrough](11-end-to-end.md)**
— a single underwriting run traced from `python -m harness.cli demo underwriting_bind`
through every layer, with every record produced, and a map of where to dig deeper.
