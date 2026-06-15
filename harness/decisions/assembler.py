"""Decision logging — the canonical governance record.

Two pieces, mirroring Verity's writer/assembler split:

  - `DecisionRecord` + `DecisionAssembler`: accumulate the canonical record
    throughout a run (ADR-0016 Category C "decision log assembler"). The
    assembler is handed bits as the run progresses and produces one record at
    the end.

  - `DecisionSink`: persists the record. Two implementations (design D4):
      * FileSink (DEFAULT) writes ADR-0015's per-run artifact layout —
        decision_log.json + model_invocations.jsonl — under
        {root}/runs/{yyyy}/{mm}/{dd}/{run_id}/. Zero infra; the exact shape
        the Verity hub ingests later, so adoption is a sink swap.
      * PostgresSink writes a row shaped like Verity's agent_decision_log.

The record field set is deliberately the v1 31-column shape (flat scalars +
nested JSON), so the file the engine writes today and the hub row it becomes
later are the same record.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from harness.core.ir import Message, Usage

logger = logging.getLogger("harness.decisions")


class DecisionRecord(BaseModel):
    """Canonical per-run governance record (Verity agent_decision_log shape)."""
    id: UUID
    entity_kind: str
    entity_name: str
    entity_version: str
    channel: str
    mock_mode: bool = False

    # correlation
    workflow_run_id: Optional[UUID] = None
    execution_run_id: Optional[UUID] = None
    parent_decision_id: Optional[UUID] = None
    decision_depth: int = 0
    step_name: Optional[str] = None
    reproduced_from_decision_id: Optional[UUID] = None

    # io
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] = Field(default_factory=dict)
    reasoning_text: Optional[str] = None

    # model / cost
    model_chain: list[dict] = Field(default_factory=list)   # the configured chain
    models_used: list[str] = Field(default_factory=list)    # what actually ran
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0

    # nested audit detail
    message_history: list[dict] = Field(default_factory=list)
    tool_calls_made: list[dict] = Field(default_factory=list)
    source_resolutions: list[dict] = Field(default_factory=list)
    target_writes: list[dict] = Field(default_factory=list)

    # governance
    application: str = "harness"
    status: str = "complete"
    error_message: Optional[str] = None
    hitl_required: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DecisionAssembler:
    """Accumulates a DecisionRecord as a run progresses. The loop hands it
    pieces; `.finish()` returns the completed record.
    """

    def __init__(self, record: DecisionRecord):
        self.record = record
        # model_invocations.jsonl is built turn-by-turn (one object per turn).
        self.model_invocations: list[dict] = []

    def add_turn(self, *, turn: int, response, neutral_blocks: list[dict]) -> None:
        """Record one model turn (for model_invocations.jsonl + cost rollup)."""
        u: Usage = response.usage
        self.record.input_tokens += u.input_tokens
        self.record.output_tokens += u.output_tokens
        self.record.cache_read_tokens += u.cache_read_tokens
        self.record.cache_write_tokens += u.cache_write_tokens
        if response.model and response.model not in self.record.models_used:
            self.record.models_used.append(response.model)
        self.model_invocations.append({
            "turn": turn,
            "provider": response.provider,
            "model": response.model,
            "request_id": response.request_id,
            "stop_reason": response.stop_reason.value,
            "usage": u.model_dump(),
            "blocks": neutral_blocks,
        })

    def add_tool_call(self, record: dict) -> None:
        self.record.tool_calls_made.append(record)

    def add_source_resolution(self, record: dict) -> None:
        self.record.source_resolutions.append(record)

    def add_target_write(self, record: dict) -> None:
        self.record.target_writes.append(record)

    def set_messages(self, messages: list[Message]) -> None:
        self.record.message_history = [m.model_dump() for m in messages]

    def finish(self, *, output: dict, reasoning: Optional[str], status: str,
               error: Optional[str], duration_ms: int) -> DecisionRecord:
        self.record.output_json = output
        self.record.reasoning_text = reasoning
        self.record.status = status
        self.record.error_message = error
        self.record.duration_ms = duration_ms
        return self.record


# ──────────────────────────────────────────────────────────────────────
# SINKS
# ──────────────────────────────────────────────────────────────────────

class DecisionSink(Protocol):
    async def write(self, assembler: DecisionAssembler) -> UUID: ...


class FileSink:
    """ADR-0015 per-run artifact layout. DEFAULT sink — zero infrastructure.

    Layout:  {root}/runs/{yyyy}/{mm}/{dd}/{run_id}/
               decision_log.json        — the DecisionRecord
               model_invocations.jsonl  — one JSON object per model turn
    """

    def __init__(self, root: str = "./_artifacts"):
        self.root = Path(root)

    async def write(self, assembler: DecisionAssembler) -> UUID:
        rec = assembler.record
        d = rec.created_at
        run_dir = self.root / "runs" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / str(rec.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "decision_log.json").write_text(
            json.dumps(rec.model_dump(mode="json"), indent=2, default=str)
        )
        with (run_dir / "model_invocations.jsonl").open("w") as f:
            for inv in assembler.model_invocations:
                f.write(json.dumps(inv, default=str) + "\n")
        logger.info("decision written: %s", run_dir / "decision_log.json")
        return rec.id

    # Replay helper: load a prior run's recorded model invocations so the
    # MockContext.model_replay path can reproduce it deterministically.
    def load_model_invocations(self, run_id: str | UUID) -> list[dict]:
        for path in self.root.rglob(f"{run_id}/model_invocations.jsonl"):
            return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        raise FileNotFoundError(f"no model_invocations.jsonl for run {run_id} under {self.root}")


class PostgresSink:
    """Optional sink writing a row shaped like Verity's agent_decision_log.

    psycopg is imported lazily so the offline/file path needs no DB driver.
    The INSERT targets a table created by docker/initdb/decisions.sql.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn

    async def write(self, assembler: DecisionAssembler) -> UUID:
        import psycopg  # lazy
        rec = assembler.record
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(
                """
                INSERT INTO agent_decision_log
                    (id, entity_kind, entity_name, entity_version, channel, mock_mode,
                     parent_decision_id, decision_depth, step_name,
                     input_json, output_json, reasoning_text,
                     models_used, input_tokens, output_tokens, duration_ms,
                     message_history, tool_calls_made, source_resolutions, target_writes,
                     application, status, error_message, hitl_required, created_at)
                VALUES
                    (%(id)s, %(entity_kind)s, %(entity_name)s, %(entity_version)s, %(channel)s, %(mock_mode)s,
                     %(parent_decision_id)s, %(decision_depth)s, %(step_name)s,
                     %(input_json)s, %(output_json)s, %(reasoning_text)s,
                     %(models_used)s, %(input_tokens)s, %(output_tokens)s, %(duration_ms)s,
                     %(message_history)s, %(tool_calls_made)s, %(source_resolutions)s, %(target_writes)s,
                     %(application)s, %(status)s, %(error_message)s, %(hitl_required)s, %(created_at)s)
                """,
                {
                    "id": str(rec.id), "entity_kind": rec.entity_kind,
                    "entity_name": rec.entity_name, "entity_version": rec.entity_version,
                    "channel": rec.channel, "mock_mode": rec.mock_mode,
                    "parent_decision_id": str(rec.parent_decision_id) if rec.parent_decision_id else None,
                    "decision_depth": rec.decision_depth, "step_name": rec.step_name,
                    "input_json": json.dumps(rec.input_json), "output_json": json.dumps(rec.output_json),
                    "reasoning_text": rec.reasoning_text,
                    "models_used": json.dumps(rec.models_used),
                    "input_tokens": rec.input_tokens, "output_tokens": rec.output_tokens,
                    "duration_ms": rec.duration_ms,
                    "message_history": json.dumps(rec.message_history, default=str),
                    "tool_calls_made": json.dumps(rec.tool_calls_made, default=str),
                    "source_resolutions": json.dumps(rec.source_resolutions, default=str),
                    "target_writes": json.dumps(rec.target_writes, default=str),
                    "application": rec.application, "status": rec.status,
                    "error_message": rec.error_message, "hitl_required": rec.hitl_required,
                    "created_at": rec.created_at,
                },
            )
            await conn.commit()
        logger.info("decision written to postgres: %s", rec.id)
        return rec.id


__all__ = ["DecisionRecord", "DecisionAssembler", "DecisionSink", "FileSink", "PostgresSink"]
