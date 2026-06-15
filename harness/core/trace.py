"""Rich tracer — watch the loop breathe (design decision D10).

Renders each ExecutionEvent as a coloured, indented line (indentation =
delegation depth, so sub-agents nest visually). With `step=True` it pauses
after each event and waits for Enter, so you can single-step the flow on the
command line. Because every stage emits a structured ExecutionEvent rather
than a print, the same events drive the trace, the decision-event log, and —
crucially — set clean breakpoints: drop a VSCode breakpoint in `Tracer.emit`
(or in any gateway) and you stop exactly at the stage you care about, with the
full neutral state in scope.

The tracer is intentionally a passive observer: the engine calls
`tracer.emit(event)` and never depends on what the tracer does with it. A
no-op tracer (`Tracer(enabled=False)`) makes the engine silent for production.
"""

from __future__ import annotations

from typing import Optional

from harness.core.result import EventType, ExecutionEvent

try:
    from rich.console import Console
    from rich.text import Text
    _RICH = True
except Exception:  # rich is optional; degrade to plain prints
    _RICH = False


_COLOR = {
    EventType.RUN_STARTED: "bold cyan",
    EventType.SOURCE_RESOLVED: "green",
    EventType.TURN_STARTED: "bold white",
    EventType.MODEL_ATTEMPT: "blue",
    EventType.MODEL_RETRY: "yellow",
    EventType.MODEL_FALLTHROUGH: "bold yellow",
    EventType.MODEL_RESPONDED: "blue",
    EventType.TOOL_AUTHORIZED: "dim green",
    EventType.TOOL_DENIED: "bold red",
    EventType.TOOL_CALLED: "magenta",
    EventType.TOOL_RESULT: "dim magenta",
    EventType.DELEGATION_STARTED: "bold cyan",
    EventType.DELEGATION_FINISHED: "cyan",
    EventType.HITL_GATE: "bold yellow",
    EventType.HITL_SUSPENDED: "bold yellow",
    EventType.HITL_RESUMED: "bold green",
    EventType.QUOTA_CHECK: "dim",
    EventType.TARGET_WRITTEN: "green",
    EventType.TARGET_SUPPRESSED: "dim yellow",
    EventType.DECISION_LOGGED: "dim cyan",
    EventType.RUN_COMPLETE: "bold green",
    EventType.RUN_FAILED: "bold red",
}


class Tracer:
    def __init__(self, enabled: bool = True, step: bool = False):
        self.enabled = enabled
        self.step = step
        self._console = Console() if (_RICH and enabled) else None
        self.events: list[ExecutionEvent] = []

    def emit(self, event: ExecutionEvent) -> None:
        # Keep a full structured record regardless of rendering.
        self.events.append(event)
        if not self.enabled:
            return
        indent = "  " * event.depth
        label = event.type.value
        detail = _fmt_detail(event.detail)
        if self._console:
            color = _COLOR.get(event.type, "white")
            line = Text()
            line.append(f"{indent}● ", style=color)
            line.append(f"{label:<20}", style=color)
            if detail:
                line.append(f"  {detail}", style="dim")
            self._console.print(line)
        else:
            print(f"{indent}● {label:<20}  {detail}")
        if self.step:
            try:
                input("    ⏸  [enter to step]")
            except (EOFError, KeyboardInterrupt):
                self.step = False

    # convenience used by the ModelChain on_event hook
    def chain_event(self, kind: str, depth: int = 0, **detail):
        mapping = {
            "model_attempt": EventType.MODEL_ATTEMPT,
            "model_retry": EventType.MODEL_RETRY,
            "model_fallthrough": EventType.MODEL_FALLTHROUGH,
        }
        et = mapping.get(kind)
        if et:
            self.emit(ExecutionEvent(type=et, depth=depth, detail=detail))


def _fmt_detail(detail: dict) -> str:
    if not detail:
        return ""
    parts = []
    for k, v in detail.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s}")
    return " ".join(parts)


__all__ = ["Tracer"]
