"""Mock model provider — offline execution and deterministic replay.

This is not a throwaway test stub; it is load-bearing (design decision D7).
The same mechanism that lets the engine run with zero API keys also powers
audit reproduction: replay a past run's recorded `model_invocations.jsonl`
and you get bit-identical model turns, so the whole decision can be
reconstructed deterministically.

Two modes:

  1. SCRIPTED — you hand it a list of `ScriptedTurn`s. Each turn either emits
     text (optionally JSON for a task's structured output) or requests one or
     more tool calls. The loop drives through them in order. This is how the
     examples and tests run without a network.

  2. REPLAY — you hand it recorded model-invocation records (the same shape
     the FileSink writes). It returns them in order, reproducing a prior run.

Because it implements the same `ModelProvider` protocol as the real adapters,
the loop cannot tell the difference — which is the point.
"""

from __future__ import annotations

import itertools
from typing import Any, Optional

from pydantic import BaseModel, Field

from harness.core.ir import (
    Message, ModelResponse, StopReason, TextBlock, ThinkingBlock,
    ToolDef, ToolUseBlock, Usage,
)
from harness.core.package import ModelRef


class ScriptedToolCall(BaseModel):
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ScriptedTurn(BaseModel):
    """One pre-canned model turn.

    - If `tool_calls` is non-empty, the turn stops with TOOL_USE and requests
      those tools. The loop will execute them and come back for the next turn.
    - Otherwise it stops with END, emitting `text` (and optional `thinking`).
      For a task, `text` is typically the JSON structured output.
    """
    thinking: Optional[str] = None
    text: Optional[str] = None
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)


class MockProvider:
    """A ModelProvider that returns scripted or replayed turns.

    Pass EITHER `turns` (scripted) OR `replay` (recorded invocation dicts).
    `default_text` is returned once the script is exhausted, so a loop that
    runs longer than the script still terminates cleanly instead of raising.
    """

    name = "mock"

    def __init__(
        self,
        turns: Optional[list[ScriptedTurn]] = None,
        replay: Optional[list[dict]] = None,
        default_text: str = '{"status": "mock-exhausted"}',
    ):
        self._turns = list(turns or [])
        self._replay = list(replay or [])
        self._default_text = default_text
        self._counter = itertools.count()

    async def complete(
        self,
        *,
        ref: ModelRef,
        system: str,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        force_tool: Optional[str] = None,
    ) -> ModelResponse:
        idx = next(self._counter)

        # REPLAY mode wins if recorded turns are present.
        if self._replay:
            if idx < len(self._replay):
                return self._from_record(self._replay[idx], ref)
            return self._terminal(ref, idx)

        # When the loop forces a specific tool (task structured_output, or the
        # agent's post-loop submit_output), honour it: emit that tool call with
        # whatever the next scripted turn carries as its input, else {}.
        if force_tool:
            payload: dict[str, Any] = {}
            if idx < len(self._turns) and self._turns[idx].tool_calls:
                payload = self._turns[idx].tool_calls[0].input
            elif idx < len(self._turns) and self._turns[idx].text:
                # let a scripted JSON text stand in as the structured payload
                payload = _maybe_json(self._turns[idx].text)
            return ModelResponse(
                blocks=[ToolUseBlock(id=f"mock-{idx}", name=force_tool, input=payload)],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(input_tokens=10, output_tokens=10),
                model=ref.model, provider="mock", request_id=f"mock-req-{idx}",
                raw_stop_reason="forced_tool",
            )

        if idx < len(self._turns):
            return self._from_turn(self._turns[idx], ref, idx)

        return self._terminal(ref, idx)

    # ── builders ──
    def _from_turn(self, turn: ScriptedTurn, ref: ModelRef, idx: int) -> ModelResponse:
        blocks: list = []
        if turn.thinking:
            blocks.append(ThinkingBlock(thinking=turn.thinking))
        if turn.tool_calls:
            for j, tc in enumerate(turn.tool_calls):
                blocks.append(ToolUseBlock(id=f"mock-{idx}-{j}", name=tc.name, input=tc.input))
            stop = StopReason.TOOL_USE
        else:
            blocks.append(TextBlock(text=turn.text if turn.text is not None else self._default_text))
            stop = StopReason.END
        return ModelResponse(
            blocks=blocks, stop_reason=stop,
            usage=Usage(input_tokens=10, output_tokens=10),
            model=ref.model, provider="mock", request_id=f"mock-req-{idx}",
            raw_stop_reason=stop.value,
        )

    def _from_record(self, record: dict, ref: ModelRef) -> ModelResponse:
        # Recorded records store the neutral blocks verbatim; rebuild them.
        blocks = []
        for b in record.get("blocks", []):
            t = b.get("type")
            if t == "text":
                blocks.append(TextBlock(text=b.get("text", "")))
            elif t == "thinking":
                blocks.append(ThinkingBlock(thinking=b.get("thinking", "")))
            elif t == "tool_use":
                blocks.append(ToolUseBlock(id=b["id"], name=b["name"], input=b.get("input", {})))
        u = record.get("usage", {})
        return ModelResponse(
            blocks=blocks,
            stop_reason=StopReason(record.get("stop_reason", "end")),
            usage=Usage(**u) if u else Usage(),
            model=record.get("model", ref.model), provider="mock",
            request_id=record.get("request_id"), raw_stop_reason="replay",
        )

    def _terminal(self, ref: ModelRef, idx: int) -> ModelResponse:
        return ModelResponse(
            blocks=[TextBlock(text=self._default_text)],
            stop_reason=StopReason.END,
            usage=Usage(input_tokens=1, output_tokens=1),
            model=ref.model, provider="mock", request_id=f"mock-req-{idx}",
            raw_stop_reason="exhausted",
        )


def _maybe_json(text: str):
    import json
    try:
        return json.loads(text)
    except Exception:
        return {"raw_output": text}


__all__ = ["MockProvider", "ScriptedTurn", "ScriptedToolCall"]
