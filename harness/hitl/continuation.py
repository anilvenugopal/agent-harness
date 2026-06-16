# SPDX-License-Identifier: AGPL-3.0-or-later
"""HITL continuation store — durable suspend/resume (design decision D6).

The agent loop is a resumable state machine; a HITL approval gate is just one
kind of suspend point. When the loop hits a gate it serialises the FULL
continuation — everything needed to resume the loop at the exact next turn —
to durable storage, then raises HITLSuspended. The worker catches that, marks
the run `suspended`, and releases. No worker, connection, or memory is held
while the human deliberates: a suspended run is just a row. That is the entire
scaling answer — suspended runs cost storage, not compute.

When the human decides (asynchronously, via any out-of-band path), the decision
is recorded against the continuation and the run is flipped back to runnable. A
worker re-claims it, loads the continuation, injects the decision as the
tool_result for the pending tool call, and continues the loop.

What we persist (and why it's the API-shaped neutral `messages`, NOT the audit
message_history): the neutral Messages list IS the continuation the model
gateway replays into; the audit history is for humans. We persist:
  - messages so far (neutral)              - run metadata (package, channel, input)
  - turn index, usage so far              - the pending tool_use (id, name, input)
  - tool_calls_made / source / target audit accumulated so far

Scope (per the plan): TOP-LEVEL agents only. Sub-agent HITL would require async
delegation (a synchronous delegate() blocks the parent worker), which is out of
scope for this build and documented as such.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger("harness.hitl")


class HumanDecision(BaseModel):
    """The reviewer's verdict on a gated tool call."""
    decision: str                       # "approve" | "deny" | "edit"
    edited_input: Optional[dict] = None  # for "edit": run the tool with this instead
    edited_result: Optional[Any] = None  # for "edit": skip the tool, use this result
    note: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Continuation(BaseModel):
    """The serialised loop state at a suspend point."""
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    decision_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # run metadata to rebuild the loop
    package_name: str
    package_version: str
    channel: str
    run_input: dict
    decision_depth: int = 0

    # loop state
    turn: int
    messages: list[dict]                 # neutral Message dicts
    tool_calls_made: list[dict] = Field(default_factory=list)
    source_resolutions: list[dict] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)

    # the gate
    gate_tool: str
    pending_tool_use: dict               # {id, name, input}

    # mock/suppress context in effect at suspension (so resume reproduces it).
    # None for fully-live runs; a serialised MockContext for mocked/test runs.
    mock: Optional[dict] = None

    # filled on resume
    status: str = "awaiting_decision"    # awaiting_decision | resolved
    decision: Optional[HumanDecision] = None


class ContinuationStore(Protocol):
    async def save(self, cont: Continuation) -> UUID: ...
    async def load(self, suspension_id: UUID) -> Continuation: ...
    async def record_decision(self, suspension_id: UUID, decision: HumanDecision) -> Continuation: ...
    async def list_pending(self) -> list[Continuation]: ...


class FileContinuationStore:
    """JSON-file continuation store — zero infra, mirrors the FileSink ethos."""

    def __init__(self, root: str = "./_artifacts/suspensions"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: UUID) -> Path:
        return self.root / f"{sid}.json"

    async def save(self, cont: Continuation) -> UUID:
        self._path(cont.id).write_text(json.dumps(cont.model_dump(mode="json"), indent=2, default=str))
        logger.info("continuation saved: %s (run %s, gate %s)", cont.id, cont.run_id, cont.gate_tool)
        return cont.id

    async def load(self, suspension_id: UUID) -> Continuation:
        p = self._path(suspension_id)
        if not p.exists():
            raise FileNotFoundError(f"no continuation {suspension_id}")
        return Continuation.model_validate_json(p.read_text())

    async def record_decision(self, suspension_id: UUID, decision: HumanDecision) -> Continuation:
        cont = await self.load(suspension_id)
        cont.decision = decision
        cont.status = "resolved"
        self._path(suspension_id).write_text(json.dumps(cont.model_dump(mode="json"), indent=2, default=str))
        logger.info("decision recorded on %s: %s", suspension_id, decision.decision)
        return cont

    async def list_pending(self) -> list[Continuation]:
        out = []
        for p in self.root.glob("*.json"):
            c = Continuation.model_validate_json(p.read_text())
            if c.status == "awaiting_decision":
                out.append(c)
        return out


__all__ = [
    "HumanDecision", "Continuation", "ContinuationStore", "FileContinuationStore",
]
