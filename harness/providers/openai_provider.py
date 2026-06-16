"""OpenAI adapter — Responses API.

Targets the `openai` Python SDK (AsyncOpenAI, `client.responses.create`). We
use the Responses API rather than Chat Completions because Chat Completions is
on a deprecation path; we use it in a NON-agentic way (plain function tools, no
hosted tool loop) so the harness keeps the loop and the per-call audit — see
design decision D2.

Translation:
  - neutral Messages → Responses `input` items. Text messages map to
    {role, content}; an assistant tool_use maps to a `function_call` item; a
    tool_result maps to a `function_call_output` item (correlated by call_id).
  - tools → [{type:"function", name, description, parameters}].
  - force_tool → tool_choice={"type":"function","name":...}.
  - response.output items → neutral blocks (message text, function_call →
    ToolUseBlock, reasoning → ThinkingBlock). stop reason: TOOL_USE if any
    function_call present, else END (MAX_TOKENS when truncated).

SDK shapes evolve; parsing is defensive and isolated to this file.
"""

from __future__ import annotations

import json
from typing import Optional

from harness.core.ir import (
    DocumentBlock, ImageBlock, Message, ModelResponse, StopReason, TextBlock, ThinkingBlock,
    ToolDef, ToolResultBlock, ToolUseBlock, Usage,
)
from harness.core.package import ModelRef
from harness.providers.base import FatalProviderError, RetryableProviderError


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        from openai import AsyncOpenAI  # lazy
        import openai
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._openai = openai

    async def complete(self, *, ref: ModelRef, system: str, messages: list[Message],
                       tools: Optional[list[ToolDef]] = None,
                       force_tool: Optional[str] = None) -> ModelResponse:
        params = {
            "model": ref.model,
            "input": _to_input_items(messages),
            "max_output_tokens": ref.max_tokens,
        }
        if system:
            params["instructions"] = system
        if ref.temperature is not None:
            params["temperature"] = ref.temperature
        if ref.top_p is not None:
            params["top_p"] = ref.top_p
        if tools:
            params["tools"] = [{"type": "function", "name": t.name,
                                "description": t.description, "parameters": t.input_schema}
                               for t in tools]
            params["tool_choice"] = ({"type": "function", "name": force_tool}
                                     if force_tool else "auto")
        params.update(ref.extra)  # e.g. {"reasoning": {"effort": "medium"}}

        try:
            resp = await self._client.responses.create(**params)
        except Exception as e:
            raise _map_error(self._openai, e)

        blocks: list = []
        tool_seen = False
        for item in (resp.output or []):
            itype = getattr(item, "type", None)
            if itype == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", None) in ("output_text", "text"):
                        blocks.append(TextBlock(text=getattr(c, "text", "")))
            elif itype == "function_call":
                tool_seen = True
                try:
                    args = json.loads(item.arguments) if isinstance(item.arguments, str) else (item.arguments or {})
                except Exception:
                    args = {"_raw_arguments": item.arguments}
                blocks.append(ToolUseBlock(id=getattr(item, "call_id", None) or item.id,
                                           name=item.name, input=args))
            elif itype == "reasoning":
                summary = getattr(item, "summary", None)
                if summary:
                    txt = " ".join(getattr(s, "text", str(s)) for s in summary)
                    blocks.append(ThinkingBlock(thinking=txt))

        # Fallback: if SDK exposed a convenience aggregate and we got no text.
        if not any(isinstance(b, TextBlock) for b in blocks):
            txt = getattr(resp, "output_text", "") or ""
            if txt:
                blocks.append(TextBlock(text=txt))

        stop = StopReason.TOOL_USE if tool_seen else StopReason.END
        if getattr(resp, "status", None) == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
            if reason == "max_output_tokens":
                stop = StopReason.MAX_TOKENS

        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0,
        ) if u else Usage()
        return ModelResponse(
            blocks=blocks, stop_reason=stop, usage=usage,
            model=getattr(resp, "model", ref.model), provider="openai",
            request_id=getattr(resp, "id", None), raw_stop_reason=getattr(resp, "status", None),
        )


def _to_input_items(messages: list[Message]) -> list[dict]:
    import base64
    import logging as _log
    _logger = _log.getLogger("harness.providers.openai")
    items: list[dict] = []
    for m in messages:
        text_parts, image_blocks, tool_uses, tool_results = [], [], [], []
        for b in m.content:
            if isinstance(b, TextBlock):
                text_parts.append(b.text)
            elif isinstance(b, ImageBlock):
                image_blocks.append(b)
            elif isinstance(b, DocumentBlock):
                # OpenAI Responses API has no inline PDF support; degrade to text.
                if b.media_type.startswith("text/"):
                    text = base64.b64decode(b.data_b64).decode("utf-8", errors="replace")
                    label = b.title or "document"
                    text_parts.append(f"[{label}]\n{text}")
                else:
                    _logger.warning("OpenAI does not support inline %s documents; skipping block", b.media_type)
            elif isinstance(b, ToolUseBlock):
                tool_uses.append(b)
            elif isinstance(b, ToolResultBlock):
                tool_results.append(b)
            # ThinkingBlock dropped on outbound
        role = "assistant" if m.role == "assistant" else "user"
        if text_parts or image_blocks:
            if image_blocks:
                # Vision: use array content format.
                content_parts: list = []
                if text_parts:
                    content_parts.append({"type": "input_text", "text": "\n".join(text_parts)})
                for img in image_blocks:
                    content_parts.append({
                        "type": "input_image",
                        "image_url": f"data:{img.media_type};base64,{img.data_b64}",
                    })
                items.append({"role": role, "content": content_parts})
            else:
                items.append({"role": role, "content": "\n".join(text_parts)})
        for tu in tool_uses:
            items.append({"type": "function_call", "call_id": tu.id, "name": tu.name,
                          "arguments": json.dumps(tu.input, default=str)})
        for tr in tool_results:
            items.append({"type": "function_call_output", "call_id": tr.tool_use_id,
                          "output": tr.content if isinstance(tr.content, str)
                          else json.dumps(tr.content, default=str)})
    return items


def _map_error(openai, e: Exception) -> Exception:
    retryable = tuple(c for c in (
        getattr(openai, "RateLimitError", None),
        getattr(openai, "InternalServerError", None),
        getattr(openai, "APIConnectionError", None),
        getattr(openai, "APITimeoutError", None),
    ) if c)
    fatal = tuple(c for c in (
        getattr(openai, "AuthenticationError", None),
        getattr(openai, "PermissionDeniedError", None),
        getattr(openai, "BadRequestError", None),
        getattr(openai, "NotFoundError", None),
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
    return RetryableProviderError(f"unclassified openai error: {e}")


__all__ = ["OpenAIProvider"]
