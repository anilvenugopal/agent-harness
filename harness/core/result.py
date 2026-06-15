"""Execution result + event types, and the HITL suspension signal.

`ExecutionResult` is what a run returns to its caller (worker, CLI, or a
parent agent during delegation). `ExecutionEvent` is the structured trace the
Rich tracer renders and the decision-event log records. `HITLSuspended` is the
control-flow signal the loop raises when it hits an approval gate — caught by
the worker, which checkpoints and releases.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from harness.core.ir import Usage


class RunStatus(str, enum.Enum):
    COMPLETE = "complete"
    FAILED = "failed"
    SUSPENDED = "suspended"      # awaiting a HITL decision
    MAX_TURNS = "max_turns"      # loop hit its turn budget still wanting tools


class ExecutionResult(BaseModel):
    run_id: UUID
    entity_kind: str
    entity_name: str
    status: RunStatus
    output: dict[str, Any] = Field(default_factory=dict)
    reasoning_text: Optional[str] = None
    decision_log_id: Optional[UUID] = None
    usage: Usage = Field(default_factory=Usage)
    duration_ms: int = 0
    error_message: Optional[str] = None
    # When status is SUSPENDED, this is the continuation id the resume path uses.
    suspension_id: Optional[UUID] = None


class EventType(str, enum.Enum):
    RUN_STARTED = "run_started"
    SOURCE_RESOLVED = "source_resolved"
    TURN_STARTED = "turn_started"
    MODEL_ATTEMPT = "model_attempt"
    MODEL_RETRY = "model_retry"
    MODEL_FALLTHROUGH = "model_fallthrough"
    MODEL_RESPONDED = "model_responded"
    TOOL_AUTHORIZED = "tool_authorized"
    TOOL_DENIED = "tool_denied"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    DELEGATION_STARTED = "delegation_started"
    DELEGATION_FINISHED = "delegation_finished"
    HITL_GATE = "hitl_gate"
    HITL_SUSPENDED = "hitl_suspended"
    HITL_RESUMED = "hitl_resumed"
    QUOTA_CHECK = "quota_check"
    TARGET_WRITTEN = "target_written"
    TARGET_SUPPRESSED = "target_suppressed"
    DECISION_LOGGED = "decision_logged"
    RUN_COMPLETE = "run_complete"
    RUN_FAILED = "run_failed"


class ExecutionEvent(BaseModel):
    type: EventType
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: Optional[UUID] = None
    depth: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class HITLSuspended(Exception):
    """Raised inside the loop when an approval gate fires.

    Carries the continuation id so the worker can mark the run suspended and
    persist the checkpoint. NOT an error — a normal control-flow pause.
    """
    def __init__(self, suspension_id: UUID, run_id: UUID, gate_tool: str):
        self.suspension_id = suspension_id
        self.run_id = run_id
        self.gate_tool = gate_tool
        super().__init__(f"HITL suspension {suspension_id} on tool {gate_tool!r}")


__all__ = ["RunStatus", "ExecutionResult", "EventType", "ExecutionEvent", "HITLSuspended"]
