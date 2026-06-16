# SPDX-License-Identifier: AGPL-3.0-or-later
"""Neutral Intermediate Representation (IR).

This is the spine of the whole engine. The agentic loop, the tool gateway,
the decision-log assembler, and the HITL continuation store all speak THIS
vocabulary — never a vendor SDK's. Each model provider adapter
(anthropic/openai/gemini/mock) translates between its own wire format and
these types, in both directions.

Why a neutral IR rather than "normalize to Anthropic's shape" or a library
like LiteLLM (design decision D1):

  - The loop must never import `anthropic`/`openai`/`google.genai`. If it
    did, swapping providers or adding a fallback would ripple through the
    loop. With the IR, the loop only ever reads four facts off a response:
    its text, its tool-call requests, why it stopped, and what it cost.

  - Per-call audit fidelity is the product. A general abstraction library
    hides exactly the things a governance system needs (each provider's
    token accounting, cache hits, per-call ids, reasoning traces). The IR
    keeps those as first-class, normalized fields.

Everything here is a plain pydantic model so it serialises straight into
the decision log and the HITL continuation blob with no custom encoders.
"""

from __future__ import annotations

import enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────
# CONTENT BLOCKS
#
# A message's content is a list of typed blocks. This mirrors the reality
# that all three providers return *structured* content (text + tool calls +
# reasoning), not a flat string. Keeping blocks lets us round-trip a
# conversation through any provider without semantic loss — which is what
# makes deterministic replay (audit reproduction) possible.
# ──────────────────────────────────────────────────────────────────────

class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    """Extended-reasoning / chain-of-thought block.

    Captured for the audit trail but never fed back to a *different*
    provider on resume (reasoning blocks are provider-private). The loop
    treats these as reasoning, not as answer.
    """
    type: Literal["thinking"] = "thinking"
    thinking: str


class ToolUseBlock(BaseModel):
    """A model's *request* to call a tool. The id correlates the eventual
    ToolResultBlock back to this request — every provider has such an id
    (Anthropic `tool_use.id`, OpenAI `call_id`, Gemini per-call id on G3),
    so we make it mandatory and provider-neutral.
    """
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """The result of executing a ToolUseBlock, to be sent back to the model.

    `content` is JSON-serialisable (string or dict). `is_error=True` tells
    the model the tool failed without aborting the run — the model decides
    how to recover. This is how denied HITL gates, connector failures, and
    unauthorized-tool refusals all flow back into the loop.
    """
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Any
    is_error: bool = False


class ImageBlock(BaseModel):
    """An inline image sent as part of a user message.

    `data_b64` holds the raw image bytes base64-encoded. Storing as str rather
    than bytes keeps every block JSON-serialisable so messages round-trip through
    the decision log and HITL continuation store with no custom encoders.
    Supported media types: image/jpeg, image/png, image/gif, image/webp.
    """
    type: Literal["image"] = "image"
    media_type: str    # "image/jpeg" | "image/png" | "image/gif" | "image/webp"
    data_b64: str      # base64-encoded raw bytes, no data: prefix
    title: Optional[str] = None


class DocumentBlock(BaseModel):
    """A document (PDF or plain text) sent as part of a user message.

    `data_b64` holds the file bytes base64-encoded. For text/* media types
    provider adapters decode to UTF-8 before sending. For application/pdf
    Anthropic and Gemini accept the base64 directly; OpenAI degrades to text.
    """
    type: Literal["document"] = "document"
    media_type: str    # "application/pdf" | "text/plain" | "text/markdown" | ...
    data_b64: str      # base64-encoded raw bytes
    title: Optional[str] = None


ContentBlock = Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, ImageBlock, DocumentBlock]


class Message(BaseModel):
    """One turn in the conversation. `role` is the neutral set; adapters map
    it to each provider's role vocabulary. Tool results are carried as
    blocks inside a `user` (or `tool`) message, matching how the loop
    appends them.
    """
    role: Literal["system", "user", "assistant", "tool"]
    content: list[ContentBlock]

    @classmethod
    def user_text(cls, text: str) -> "Message":
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant_blocks(cls, blocks: list[ContentBlock]) -> "Message":
        return cls(role="assistant", content=blocks)

    @classmethod
    def tool_results(cls, results: list[ToolResultBlock]) -> "Message":
        # Tool results go back as a user-role message carrying result blocks.
        # (Anthropic wants role=user; OpenAI/Gemini adapters re-key as needed.)
        return cls(role="user", content=list(results))


# ──────────────────────────────────────────────────────────────────────
# TOOL DEFINITIONS
# ──────────────────────────────────────────────────────────────────────

class ToolDef(BaseModel):
    """A tool advertised to the model. `input_schema` is JSON Schema. Each
    adapter wraps this in the provider's required envelope (Anthropic:
    flat name/description/input_schema; OpenAI Responses: {type:function,...};
    Gemini: function_declarations[]).
    """
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


# ──────────────────────────────────────────────────────────────────────
# RESPONSE
# ──────────────────────────────────────────────────────────────────────

class StopReason(str, enum.Enum):
    """Normalized stop reasons. The loop branches only on these — never on a
    provider's raw string. `END` = model finished its turn with an answer;
    `TOOL_USE` = model wants tools run; `MAX_TOKENS` = truncated.
    """
    END = "end"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    OTHER = "other"


class Usage(BaseModel):
    """Normalized token accounting. Cache fields are 0 when a provider
    doesn't expose them. Every adapter populates this from its own usage
    object so the cost layer is provider-agnostic.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class ModelResponse(BaseModel):
    """The neutral result of a single model turn — the ONLY thing the loop
    reads back from a provider. Adapters build this; the loop consumes it.
    """
    blocks: list[ContentBlock]
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)
    # Provider bookkeeping for the audit trail (request id, model string,
    # provider name, raw stop string). Never drives control flow.
    model: str = ""
    provider: str = ""
    request_id: Optional[str] = None
    raw_stop_reason: Optional[str] = None

    # ── convenience accessors the loop uses ──
    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def thinking(self) -> str:
        return "\n".join(b.thinking for b in self.blocks if isinstance(b, ThinkingBlock))

    @property
    def tool_calls(self) -> list[ToolUseBlock]:
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]


__all__ = [
    "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock",
    "ImageBlock", "DocumentBlock",
    "ContentBlock", "Message", "ToolDef",
    "StopReason", "Usage", "ModelResponse",
]
