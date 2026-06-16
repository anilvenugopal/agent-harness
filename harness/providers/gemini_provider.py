"""Gemini adapter — google-genai SDK.

Targets the unified `google-genai` SDK (`from google import genai`), using
`client.aio.models.generate_content`. Imported lazily.

Gemini specifics the adapter absorbs:
  - roles are "user" / "model" (assistant → "model").
  - a tool call is a Part with a function_call (name, args[, id on Gemini 3]).
  - a tool RESULT is a Part with a function_response correlated by function
    NAME (historically) — so we keep an id→name map from the assistant
    tool_use blocks to fill the name when translating a neutral ToolResultBlock.
  - forcing a tool uses ToolConfig(FunctionCallingConfig(mode="ANY",
    allowed_function_names=[...])).
  - usage_metadata exposes prompt/candidates/cached token counts.

This file owns all Gemini coupling; the loop never sees it.
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


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str):
        from google import genai  # lazy
        from google.genai import types
        self._genai = genai
        self._types = types
        self._client = genai.Client(api_key=api_key)

    async def complete(self, *, ref: ModelRef, system: str, messages: list[Message],
                       tools: Optional[list[ToolDef]] = None,
                       force_tool: Optional[str] = None) -> ModelResponse:
        types = self._types
        id_to_name = _id_name_map(messages)
        contents = [_to_content(types, m, id_to_name) for m in messages]

        config_kwargs = {"max_output_tokens": ref.max_tokens}
        if system:
            config_kwargs["system_instruction"] = system
        if ref.temperature is not None:
            config_kwargs["temperature"] = ref.temperature
        if ref.top_p is not None:
            config_kwargs["top_p"] = ref.top_p
        if tools:
            config_kwargs["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description,
                                          parameters=t.input_schema) for t in tools])]
            if force_tool:
                config_kwargs["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY", allowed_function_names=[force_tool]))
        config_kwargs.update(ref.extra)

        try:
            resp = await self._client.aio.models.generate_content(
                model=ref.model, contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as e:
            raise _map_error(e)

        blocks: list = []
        tool_seen = False
        finish_reason = None
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            cand = candidates[0]
            finish_reason = getattr(cand, "finish_reason", None)
            parts = getattr(getattr(cand, "content", None), "parts", None) or []
            for p in parts:
                fc = getattr(p, "function_call", None)
                if fc is not None:
                    tool_seen = True
                    blocks.append(ToolUseBlock(
                        id=getattr(fc, "id", None) or f"gemini-{fc.name}",
                        name=fc.name, input=dict(fc.args or {})))
                elif getattr(p, "thought", False) and getattr(p, "text", None):
                    blocks.append(ThinkingBlock(thinking=p.text))
                elif getattr(p, "text", None):
                    blocks.append(TextBlock(text=p.text))

        stop = StopReason.TOOL_USE if tool_seen else StopReason.END
        if str(finish_reason).upper().endswith("MAX_TOKENS"):
            stop = StopReason.MAX_TOKENS

        um = getattr(resp, "usage_metadata", None)
        usage = Usage(
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
            cache_read_tokens=getattr(um, "cached_content_token_count", 0) or 0,
        ) if um else Usage()
        return ModelResponse(
            blocks=blocks, stop_reason=stop, usage=usage,
            model=ref.model, provider="gemini",
            request_id=getattr(resp, "response_id", None), raw_stop_reason=str(finish_reason),
        )


def _id_name_map(messages: list[Message]) -> dict[str, str]:
    m: dict[str, str] = {}
    for msg in messages:
        for b in msg.content:
            if isinstance(b, ToolUseBlock):
                m[b.id] = b.name
    return m


def _to_content(types, m: Message, id_to_name: dict[str, str]):
    import base64
    role = "model" if m.role == "assistant" else "user"
    parts = []
    for b in m.content:
        if isinstance(b, TextBlock):
            parts.append(types.Part(text=b.text))
        elif isinstance(b, ImageBlock):
            parts.append(types.Part(inline_data=types.Blob(
                mime_type=b.media_type,
                data=base64.b64decode(b.data_b64),
            )))
        elif isinstance(b, DocumentBlock):
            parts.append(types.Part(inline_data=types.Blob(
                mime_type=b.media_type,
                data=base64.b64decode(b.data_b64),
            )))
        elif isinstance(b, ToolUseBlock):
            parts.append(types.Part(function_call=types.FunctionCall(name=b.name, args=b.input)))
        elif isinstance(b, ToolResultBlock):
            name = id_to_name.get(b.tool_use_id, b.tool_use_id)
            resp = b.content if isinstance(b.content, dict) else {"result": b.content}
            parts.append(types.Part(function_response=types.FunctionResponse(name=name, response=resp)))
        # ThinkingBlock dropped on outbound
    return types.Content(role=role, parts=parts)


def _map_error(e: Exception) -> Exception:
    # google.genai.errors.APIError carries a .code (HTTP status).
    status = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return RetryableProviderError(str(e))
    if isinstance(status, int) and 400 <= status < 500:
        return FatalProviderError(str(e))
    name = type(e).__name__.lower()
    if "server" in name or "timeout" in name or "connection" in name or "unavailable" in name:
        return RetryableProviderError(str(e))
    return RetryableProviderError(f"unclassified gemini error: {e}")


__all__ = ["GeminiProvider"]
