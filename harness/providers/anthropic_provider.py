"""Anthropic (Claude) adapter.

Targets the `anthropic` Python SDK (AsyncAnthropic, Messages API). The SDK is
imported lazily so the offline path never needs it installed.

Translation responsibilities:
  - neutral Messages → Anthropic message params (system is a separate arg;
    thinking blocks are dropped on the outbound path — they are provider-private
    reasoning, not conversation we replay to a *different* provider).
  - tools → Anthropic's flat {name, description, input_schema} shape.
  - force_tool → tool_choice={"type":"tool","name":...}.
  - Anthropic response.content blocks → neutral blocks; stop_reason + usage
    (incl. prompt-cache fields) → neutral StopReason + Usage.
  - transient errors (429/5xx/overloaded) → RetryableProviderError;
    auth/bad-request → FatalProviderError. The chain decides retry vs fallback.

NOTE: API shapes evolve. If a future SDK changes block/usage attribute names,
this is the only file that needs to change — the loop is unaffected.
"""

from __future__ import annotations

from typing import Optional

from harness.core.ir import (
    Message, ModelResponse, StopReason, TextBlock, ThinkingBlock,
    ToolDef, ToolResultBlock, ToolUseBlock, Usage,
)
from harness.core.package import ModelRef
from harness.providers.base import FatalProviderError, RetryableProviderError

_STOP = {
    "end_turn": StopReason.END,
    "stop_sequence": StopReason.END,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "pause_turn": StopReason.OTHER,
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        import anthropic  # lazy
        self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        self._anthropic = anthropic

    async def complete(self, *, ref: ModelRef, system: str, messages: list[Message],
                       tools: Optional[list[ToolDef]] = None,
                       force_tool: Optional[str] = None) -> ModelResponse:
        params = {
            "model": ref.model,
            "max_tokens": ref.max_tokens,
            "messages": [_to_anthropic_msg(m) for m in messages],
        }
        if system:
            params["system"] = system
        if ref.temperature is not None:
            params["temperature"] = ref.temperature
        if ref.top_p is not None:
            params["top_p"] = ref.top_p
        if tools:
            params["tools"] = [{"name": t.name, "description": t.description,
                                "input_schema": t.input_schema} for t in tools]
            params["tool_choice"] = ({"type": "tool", "name": force_tool}
                                     if force_tool else {"type": "auto"})
        params.update(ref.extra)  # e.g. {"thinking": {"type": "enabled", "budget_tokens": ...}}

        try:
            resp = await self._client.messages.create(**params)
        except Exception as e:  # map to chain's retry/fatal taxonomy
            raise _map_error(self._anthropic, e)

        blocks: list = []
        for b in resp.content:
            bt = getattr(b, "type", None)
            if bt == "text":
                blocks.append(TextBlock(text=b.text))
            elif bt == "thinking":
                blocks.append(ThinkingBlock(thinking=getattr(b, "thinking", "")))
            elif bt == "tool_use":
                blocks.append(ToolUseBlock(id=b.id, name=b.name, input=dict(b.input or {})))
        u = resp.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return ModelResponse(
            blocks=blocks, stop_reason=_STOP.get(resp.stop_reason, StopReason.OTHER),
            usage=usage, model=resp.model, provider="anthropic",
            request_id=getattr(resp, "id", None), raw_stop_reason=resp.stop_reason,
        )


def _to_anthropic_msg(m: Message) -> dict:
    # Anthropic uses roles user/assistant only; tool results ride inside a user msg.
    role = "assistant" if m.role == "assistant" else "user"
    content = []
    for b in m.content:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            content.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                            "content": _stringify(b.content), "is_error": b.is_error})
        # ThinkingBlock intentionally dropped on outbound.
    return {"role": role, "content": content}


def _stringify(content) -> str:
    import json
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _map_error(anthropic, e: Exception) -> Exception:
    # Use SDK exception classes when available; fall back to status-code sniffing.
    retryable = tuple(c for c in (
        getattr(anthropic, "RateLimitError", None),
        getattr(anthropic, "InternalServerError", None),
        getattr(anthropic, "APIConnectionError", None),
        getattr(anthropic, "APITimeoutError", None),
    ) if c)
    fatal = tuple(c for c in (
        getattr(anthropic, "AuthenticationError", None),
        getattr(anthropic, "PermissionDeniedError", None),
        getattr(anthropic, "BadRequestError", None),
        getattr(anthropic, "NotFoundError", None),
    ) if c)
    if retryable and isinstance(e, retryable):
        return RetryableProviderError(str(e))
    if fatal and isinstance(e, fatal):
        return FatalProviderError(str(e))
    status = getattr(e, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return RetryableProviderError(str(e))
    if isinstance(status, int) and 400 <= status < 500:
        return FatalProviderError(str(e))
    return RetryableProviderError(f"unclassified anthropic error: {e}")


__all__ = ["AnthropicProvider"]
