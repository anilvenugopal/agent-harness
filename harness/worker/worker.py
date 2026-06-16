# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worker — the worker-mode dispatch path (Postgres SKIP LOCKED).

This is the v1 claim loop, which ADR-0015 explicitly preserves as the
`VERITY_DISPATCH_MODE=postgres` fallback. It is the stub for the distributed
plane: instead of a NATS-fed coordinator handing runs to workers, this worker
claims runs straight off a Postgres queue. ADR-0010's invariant — "in-flight
execution never depends on the coordinator" — means the engine inside this
worker is byte-identical to what runs under NATS later; only the CLAIM SOURCE
changes. Adopting the real plane = swapping `claim_one` for a NATS consumer.

Loop per iteration:
  1. claim_one(): atomic UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
     marks one queued run `executing` and returns it (or None).
  2. dispatch to engine.run_task / run_agent by entity_kind.
  3. on success → mark complete (store decision_log_id, output).
     on HITLSuspended → mark suspended (store suspension_id) and RELEASE — no
       worker/connection/memory is held while the human deliberates.
     on error → mark failed (or requeue with attempt+1 up to max_attempts).

The Worker is generic: it takes a pre-built ExecutionEngine, so the demo can
register its own python tools / MCP servers / connectors before handing it in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Optional
from uuid import UUID

from harness.core.engine import ExecutionEngine
from harness.core.result import HITLSuspended, RunStatus

logger = logging.getLogger("harness.worker")


class Worker:
    def __init__(self, engine: ExecutionEngine, dsn: str, *,
                 worker_id: str = "worker-1", poll_interval: float = 1.0,
                 max_attempts: int = 3):
        self.engine = engine
        self.dsn = dsn
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        self._stop = asyncio.Event()

    def request_stop(self, *_):
        self._stop.set()

    async def run_forever(self) -> None:
        logger.info("worker %s starting (dsn=%s)", self.worker_id, _redact(self.dsn))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                pass
        while not self._stop.is_set():
            claimed = await self.claim_one()
            if claimed is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._execute(claimed)
        logger.info("worker %s stopped", self.worker_id)

    async def claim_one(self) -> Optional[dict]:
        import psycopg
        from psycopg.rows import dict_row
        async with await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row) as conn:
            cur = await conn.execute(
                """
                UPDATE execution_run SET status='executing', claimed_at=now(),
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
            if row:
                logger.info("claimed run %s (%s/%s) attempt=%s",
                            row["id"], row["entity_kind"], row["entity_name"], row["attempt"])
            return row

    async def _execute(self, run: dict) -> None:
        run_id = run["id"] if isinstance(run["id"], UUID) else UUID(str(run["id"]))
        kind = run["entity_kind"]
        name = run["entity_name"]
        inp = run["input_json"] if isinstance(run["input_json"], dict) else json.loads(run["input_json"])
        try:
            if kind == "task":
                result = await self.engine.run_task(task_name=name, input_data=inp,
                                                    channel=run["channel"], execution_run_id=run_id)
            else:
                result = await self.engine.run_agent(agent_name=name, context=inp,
                                                     channel=run["channel"], execution_run_id=run_id)
            await self._mark_terminal(run_id, result)
        except HITLSuspended as s:
            await self._mark_suspended(run_id, s.suspension_id)
        except Exception as e:
            logger.exception("run %s crashed", run_id)
            await self._mark_failed(run_id, run.get("attempt", 1), str(e))

    async def _mark_terminal(self, run_id, result) -> None:
        import psycopg
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(
                """UPDATE execution_run
                      SET status=%(s)s, decision_log_id=%(d)s, output_json=%(o)s, finished_at=now()
                    WHERE id=%(id)s""",
                {"s": result.status.value, "d": str(result.decision_log_id) if result.decision_log_id else None,
                 "o": json.dumps(result.output, default=str), "id": str(run_id)},
            )
            await conn.commit()
        logger.info("run %s → %s", run_id, result.status.value)

    async def _mark_suspended(self, run_id, suspension_id) -> None:
        import psycopg
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(
                """UPDATE execution_run SET status='suspended', suspension_id=%(sid)s WHERE id=%(id)s""",
                {"sid": str(suspension_id), "id": str(run_id)},
            )
            await conn.commit()
        logger.info("run %s → suspended (suspension %s); worker released", run_id, suspension_id)

    async def _mark_failed(self, run_id, attempt, error) -> None:
        import psycopg
        # Requeue until max_attempts, then fail terminally.
        status = "queued" if attempt < self.max_attempts else "failed"
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(
                """UPDATE execution_run SET status=%(s)s, error_message=%(e)s,
                          finished_at = CASE WHEN %(s)s='failed' THEN now() ELSE finished_at END
                    WHERE id=%(id)s""",
                {"s": status, "e": error[:2000], "id": str(run_id)},
            )
            await conn.commit()
        logger.info("run %s → %s (attempt %s)", run_id, status, attempt)


# ── enqueue + resume helpers (used by the CLI / demos) ──

async def enqueue_run(dsn: str, *, entity_kind: str, entity_name: str,
                      input_data: dict, channel: str = "production") -> UUID:
    import psycopg
    from uuid import uuid4
    rid = uuid4()
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            """INSERT INTO execution_run (id, entity_kind, entity_name, channel, input_json, status)
               VALUES (%(id)s, %(k)s, %(n)s, %(c)s, %(i)s, 'queued')""",
            {"id": str(rid), "k": entity_kind, "n": entity_name, "c": channel,
             "i": json.dumps(input_data, default=str)},
        )
        await conn.commit()
    return rid


async def requeue_resumed(dsn: str, run_id: UUID) -> None:
    """After a human decision is recorded, flip the suspended run back to
    runnable so a worker re-claims it. The worker then calls engine.resume()."""
    import psycopg
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute("UPDATE execution_run SET status='queued' WHERE id=%(id)s AND status='suspended'",
                           {"id": str(run_id)})
        await conn.commit()


def _redact(dsn: str) -> str:
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)


__all__ = ["Worker", "enqueue_run", "requeue_resumed"]
