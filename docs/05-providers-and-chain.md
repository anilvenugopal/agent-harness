# Module 05 — Providers and the Model Chain

> Key files: `harness/providers/base.py`, `harness/providers/anthropic_provider.py`,
> `harness/providers/openai_provider.py`, `harness/providers/gemini_provider.py`,
> `harness/providers/mock_provider.py`, `harness/core/factory.py`
>
> This module covers the seam between the engine and the LLM vendors: the
> `ModelProvider` protocol, the `ModelChain` retry/fallback logic, the four
> concrete adapters, and the factory that wires them together.

---

## The core abstraction: one interface, four implementations

The engine calls one method:

```python
response = await self._model_call(pkg, system, messages, tools, force_tool, depth)
```

That eventually becomes:

```python
response = await provider.complete(
    ref=link,         # which model + its parameters (model string, max_tokens, etc.)
    system=system,    # the system prompt
    messages=messages,  # neutral IR Messages — TextBlock, ToolUseBlock, ToolResultBlock, etc.
    tools=tools,      # neutral ToolDef list
    force_tool=force_tool,  # tool name to force, or None
)
```

`provider.complete` is the only interface the engine ever touches. Every vendor
difference — API shape, auth, tool encoding, error taxonomy, response parsing — is
inside the concrete adapter that implements it. The engine imports nothing from
`anthropic`, `openai`, or `google.genai`.

The `ModelProvider` protocol captures this contract:

```python
# harness/providers/base.py

@runtime_checkable
class ModelProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        ref: ModelRef,
        system: str,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        force_tool: Optional[str] = None,
    ) -> ModelResponse: ...
```

It is a structural Protocol — any class with the right signature satisfies it,
no inheritance needed. The adapters and the mock all implement it. So does any
future adapter you write (a Bedrock adapter, an Ollama adapter, etc.).

The provider abstraction creates a hard wall between the engine and every vendor:

```
┌─────────────────────────────────────────────────────────┐
│                    ENGINE / LOOP                         │
│  neutral Messages, ToolDefs, ModelResponse — IR only     │
└──────────────────────┬──────────────────────────────────┘
                       │  provider.complete(ref, system,
                       │  messages, tools, force_tool)
           ════════════╪════════ provider boundary ════════
                       │
          ┌────────────┼────────────┬───────────────────┐
          ▼            ▼            ▼                   ▼
  AnthropicProvider  OpenAI    GeminiProvider    MockProvider
  (messages API)  (Responses)  (generate_content) (scripted/replay)
       │               │            │
  anthropic SDK    openai SDK  google-genai SDK
  wire format      wire format  wire format
```

Everything below the boundary is vendor-specific. Everything above it is
provider-agnostic neutral IR. The engine never crosses the boundary; adapters
never reach upward into the engine.

---

## The chain — retry within a link, fall through across links

A package's `inference.chain` is an ordered list of `ModelRef`s:

```yaml
inference:
  fallback_enabled: true
  chain:
    - provider: anthropic
      model: claude-opus-4-8
      priority: 0
      max_tokens: 2048
    - provider: openai
      model: gpt-4.1
      priority: 1
      max_tokens: 2048
    - provider: gemini
      model: gemini-2.5-pro
      priority: 2
      max_tokens: 2048
```

`ModelChain.complete` sorts these by `priority` and works through them:

```python
# harness/providers/base.py — ModelChain.complete (condensed)

async def complete(self, *, chain, ..., fallback_enabled=False) -> ModelResponse:
    ordered = sorted(chain, key=lambda m: m.priority)
    if not fallback_enabled:
        ordered = ordered[:1]   # primary-only; fail fast

    last_error = None
    for link in ordered:
        provider = self.providers.get(link.provider)
        if provider is None:
            last_error = FatalProviderError(f"provider {link.provider!r} not registered")
            continue   # skip unregistered links

        for attempt in range(self.max_retries + 1):
            try:
                return await provider.complete(ref=link, ...)
            except RetryableProviderError as e:
                last_error = e
                if attempt < self.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                break   # retries exhausted → fall to next link
            except FatalProviderError as e:
                last_error = e
                break   # no point retrying a bad request → fall immediately

    raise ProviderError(f"chain exhausted. Last error: {last_error}")
```

The two control flows:

```
RETRYABLE (429, 5xx, network timeout):
  link 0, attempt 0 → 429 → sleep(jitter) →
  link 0, attempt 1 → 429 → sleep(jitter) →
  link 0, attempt 2 → 429 → sleep(jitter) → retries exhausted
  link 1, attempt 0 → ...

FATAL (401 auth, 400 bad request):
  link 0, attempt 0 → 401 → skip all retries
  link 1, attempt 0 → ...
```

Here is the full decision tree the chain walks for one link:

```
              ┌──────────────────────────┐
              │  provider.complete(...)  │
              └────────────┬─────────────┘
                           │
               ┌───────────▼───────────┐
               │      success?         │
               └─────┬─────────┬───────┘
                   YES         NO
                    │          │
                    ▼     ┌────▼──────────────┐
               RETURN     │ RetryableError?   │
               response   └──┬──────────┬─────┘
                           YES          NO (FatalError)
                            │           │
                    ┌───────▼──────┐    └──► skip to next link
                    │ attempts     │
                    │ remaining?   │
                    └──┬───────┬───┘
                      YES      NO
                       │        │
                  sleep(jitter) └──► fall through to next link
                  retry call
```

The critical insight: retries stay on the **same link** (same provider). Fall-through
moves to the **next link** (different provider). A flaky Anthropic server gets
retried; a persistently broken Anthropic account falls through to OpenAI.

The key design decision: **retry inside a link, fall through across links**. A
transient error (rate limit, overload) might go away if you wait — so retry the
same provider. A fatal error (bad API key, malformed request) won't go away no
matter how many times you retry — skip to the next link immediately.

---

## Error taxonomy: two classes, mapped by every adapter

```python
class RetryableProviderError(ProviderError):
    """Transient: 429 rate limit, 5xx server error, network timeout, overloaded."""

class FatalProviderError(ProviderError):
    """Permanent: 401 auth, 403 permission, 400 bad request, 404 not found."""
```

The chain only understands these two types. It does not know what an
`anthropic.RateLimitError` or `openai.AuthenticationError` is. Every adapter
translates its vendor's exceptions into one of the two types before re-raising.

The Anthropic adapter's error mapper:

```python
def _map_error(anthropic, e):
    retryable = (anthropic.RateLimitError, anthropic.InternalServerError,
                 anthropic.APIConnectionError, anthropic.APITimeoutError)
    fatal     = (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                 anthropic.BadRequestError, anthropic.NotFoundError)
    if isinstance(e, retryable): return RetryableProviderError(str(e))
    if isinstance(e, fatal):     return FatalProviderError(str(e))
    # Status-code fallback when SDK class is unavailable.
    if status == 429 or status >= 500: return RetryableProviderError(str(e))
    if 400 <= status < 500:            return FatalProviderError(str(e))
    return RetryableProviderError(f"unclassified: {e}")  # unknown → assume retryable
```

Both OpenAI and Gemini adapters follow the same pattern with their own SDK
exception classes. The "unclassified → retryable" default is conservative: better
to retry something that turns out to be fatal than to permanently give up on
something that could have worked.

---

## Backoff: full jitter

```python
def _backoff(self, attempt: int) -> float:
    ceiling = min(self.max_delay, self.base_delay * (2 ** attempt))
    return random.uniform(0, ceiling)
```

This is **full-jitter exponential backoff** — the canonical solution to the
thundering-herd problem. Without jitter:

```
Worker 1: 429 → sleep 1s → retry (at t=1)
Worker 2: 429 → sleep 1s → retry (at t=1)
Worker 3: 429 → sleep 1s → retry (at t=1)
→ all three hit the API at the same instant and get another 429
```

With full jitter, each worker picks a random sleep in `[0, ceiling]`. Even ten
workers backing off simultaneously spread their retries across the window, and the
burst dissipates. The implementation notes in `base.py` call this out explicitly:
the v1 engine had the deterministic backoff bug and this was the fix.

Default parameters: `max_retries=3`, `base_delay=1.0s`, `max_delay=30s`. That
gives backoff ceilings of 1s, 2s, 4s (each up to `max_delay=30s`).

---

## Two-tier fallback control

Whether the chain walks past link 0 is controlled by two flags working together:

| `HARNESS_FALLBACK_ENABLED` env | `inference.fallback_enabled` in package | Result |
|---|---|---|
| `false` | (anything) | Primary only — global kill switch |
| `true` | `false` (explicit) | Primary only — package opts out |
| `true` | `true` or unset | Full chain — primary + all fallbacks |

In code:

```python
# In engine.py — the engine derives per-run effective flag:
def _effective_fallback(self, pkg):
    if not self.global_fallback_enabled:
        return False
    if pkg.inference.fallback_enabled is False:
        return False
    return True

# Passed to ModelChain.complete:
await self.chain.complete(..., fallback_enabled=self._effective_fallback(pkg))
```

Why two tiers? The global flag (an env var) lets ops turn off fallback across
all agents at once — useful when the fallback providers shouldn't be used (e.g.,
in a regulated environment where only one approved provider is in the chain). The
per-package flag lets specific packages opt out even when the global switch is on
(e.g., a package that produces output only valid for one provider's extended
thinking capability).

---

## The four providers

### 1. Anthropic — `anthropic_provider.py`

Uses `anthropic.AsyncAnthropic` with the Messages API. The system prompt goes as
a separate top-level `system` parameter (not inside `messages`).

**Inbound translation — neutral messages → Anthropic wire format:**

```python
def _to_anthropic_msg(m: Message) -> dict:
    role = "assistant" if m.role == "assistant" else "user"
    content = []
    for b in m.content:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ImageBlock):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": b.media_type, "data": b.data_b64},
            })
        elif isinstance(b, DocumentBlock):
            if b.media_type == "application/pdf":
                content.append({"type": "document",
                                 "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": b.data_b64}})
            else:  # text/*
                text = base64.b64decode(b.data_b64).decode("utf-8", errors="replace")
                content.append({"type": "document",
                                 "source": {"type": "text", "media_type": b.media_type,
                                            "data": text}})
        elif isinstance(b, ToolUseBlock):
            content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            content.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                            "content": _stringify(b.content), "is_error": b.is_error})
        # ThinkingBlock intentionally dropped on outbound
    return {"role": role, "content": content}
```

**Outbound translation — Anthropic response → neutral blocks:**

```python
for b in resp.content:
    bt = getattr(b, "type", None)
    if bt == "text":       blocks.append(TextBlock(text=b.text))
    elif bt == "thinking": blocks.append(ThinkingBlock(thinking=b.thinking))
    elif bt == "tool_use": blocks.append(ToolUseBlock(id=b.id, name=b.name, input=dict(b.input or {})))
```

**Stop reason mapping:**

```python
_STOP = {
    "end_turn":     StopReason.END,
    "stop_sequence": StopReason.END,
    "tool_use":     StopReason.TOOL_USE,
    "max_tokens":   StopReason.MAX_TOKENS,
    "pause_turn":   StopReason.OTHER,
}
```

**Force tool:**

```python
params["tool_choice"] = {"type": "tool", "name": force_tool}  # if force_tool set
# else:
params["tool_choice"] = {"type": "auto"}
```

**Usage — Anthropic-specific fields for prompt caching:**

```python
usage = Usage(
    input_tokens=u.input_tokens,
    output_tokens=u.output_tokens,
    cache_read_tokens=u.cache_read_input_tokens,    # Anthropic-specific
    cache_write_tokens=u.cache_creation_input_tokens,  # Anthropic-specific
)
```

These are surfaced in the decision log's token counts. They don't affect the
engine's cost estimate unless the `QuotaEnforcer` is taught to price cache reads
differently (which is an open enhancement).

---

### 2. OpenAI — `openai_provider.py`

Uses `openai.AsyncOpenAI` with the **Responses API** (`client.responses.create`),
not Chat Completions. The system prompt is passed as `instructions` (Responses
API name for it). The engine's tool loop is explicit — the provider does not use
OpenAI's hosted tool execution loop.

**Why Responses API over Chat Completions?**

Chat Completions is on a deprecation path for new capabilities. The Responses API
is the successor and exposes `reasoning` output items (for o1/o3 reasoning models),
a cleaner tool call shape, and proper support for vision. The non-agentic usage
(no hosted loop) keeps the harness's own loop in control.

**Inbound translation — neutral messages → Responses API input items:**

```python
def _to_input_items(messages: list[Message]) -> list[dict]:
    items = []
    for m in messages:
        text_parts, image_blocks, tool_uses, tool_results = [], [], [], []
        for b in m.content:
            if isinstance(b, TextBlock):   text_parts.append(b.text)
            elif isinstance(b, ImageBlock): image_blocks.append(b)
            elif isinstance(b, DocumentBlock):
                if b.media_type.startswith("text/"):
                    text = base64.b64decode(b.data_b64).decode("utf-8", errors="replace")
                    text_parts.append(f"[{b.title or 'document'}]\n{text}")
                else:
                    logger.warning("OpenAI does not support inline %s documents", b.media_type)
            elif isinstance(b, ToolUseBlock):   tool_uses.append(b)
            elif isinstance(b, ToolResultBlock): tool_results.append(b)
        # ...
```

Several important asymmetries vs Anthropic:

| Content type | Anthropic | OpenAI |
|---|---|---|
| Text | `{type: text, text: ...}` in content array | Concatenated to `content` string |
| Image | `{type: image, source: {type: base64, ...}}` | `{type: input_image, image_url: data:...;base64,...}` |
| Document (text) | `{type: document, source: {type: text, ...}}` | Decoded and prepended as text |
| Document (PDF) | `{type: document, source: {type: base64, ...}}` | Not supported — logged warning, skipped |
| Tool call | `{type: tool_use, id, name, input}` | `{type: function_call, call_id, name, arguments}` |
| Tool result | `{type: tool_result, tool_use_id, content}` | `{type: function_call_output, call_id, output}` |

Note that tool calls and tool results are **top-level items** in the Responses API
`input` list, not nested inside a message's `content` array. So the translation
loop emits them as separate list entries rather than wrapping them in a message
dict.

**Stop reason:** The Responses API does not have an explicit stop reason field. The
adapter infers it: if any `function_call` item appears in `output`, stop_reason is
`TOOL_USE`; otherwise `END`. Truncation is signalled by `resp.status == "incomplete"`
with `incomplete_details.reason == "max_output_tokens"`.

**Force tool:**

```python
params["tool_choice"] = {"type": "function", "name": force_tool}  # OpenAI shape
```

**Reasoning output (o1/o3 models):**

```python
elif itype == "reasoning":
    summary = getattr(item, "summary", None)
    if summary:
        txt = " ".join(getattr(s, "text", str(s)) for s in summary)
        blocks.append(ThinkingBlock(thinking=txt))
```

Enable reasoning by passing `{"reasoning": {"effort": "medium"}}` in
`ref.extra`. The adapter passes `ref.extra` to the API call via
`params.update(ref.extra)`.

---

### 3. Gemini — `gemini_provider.py`

Uses `google.genai.Client` (the unified `google-genai` SDK) with
`client.aio.models.generate_content`. Several quirks vs Anthropic/OpenAI:

**Roles:** Gemini uses `"user"` and `"model"` — not `"assistant"`. The adapter
maps `m.role == "assistant"` → `role = "model"`.

**Tool result correlation by name, not id:**

This is the most significant Gemini idiosyncrasy. In Anthropic's API, a tool
result references the original tool call by `tool_use_id`. In Gemini, a
`function_response` references it by function **name**:

```python
parts.append(types.Part(function_response=types.FunctionResponse(
    name=name,    # ← function NAME, not the ID
    response=resp
)))
```

But neutral `ToolResultBlock` only stores the `tool_use_id` — not the name. To
resolve this, the adapter pre-scans the messages to build an id→name map:

```python
def _id_name_map(messages: list[Message]) -> dict[str, str]:
    m = {}
    for msg in messages:
        for b in msg.content:
            if isinstance(b, ToolUseBlock):
                m[b.id] = b.name
    return m

# Later, in _to_content:
name = id_to_name.get(b.tool_use_id, b.tool_use_id)
```

If the id is not found in the map (shouldn't happen in a well-formed history),
the id itself is used as a fallback.

**Multimodal content:** Both `ImageBlock` and `DocumentBlock` translate to
`types.Part(inline_data=types.Blob(mime_type=..., data=bytes))`. Gemini accepts
raw bytes (not base64 strings) inside `Blob.data`, so the adapter decodes:

```python
data=base64.b64decode(b.data_b64)
```

**Tool declarations:** Gemini tools are declared as `FunctionDeclaration`s inside
a `Tool` object — one `Tool` wrapping all functions, not a flat list.

**Force tool:**

```python
config_kwargs["tool_config"] = types.ToolConfig(
    function_calling_config=types.FunctionCallingConfig(
        mode="ANY",
        allowed_function_names=[force_tool],
    )
)
```

`mode="ANY"` means "you must call a tool." `allowed_function_names` restricts which
one — equivalent to Anthropic's `{"type": "tool", "name": ...}`.

**Stop reason:** Gemini surfaces `finish_reason` on `candidates[0]`. The adapter
checks if it ends with `"MAX_TOKENS"` (the exact value varies across model
generations, so the suffix check is safer than equality).

---

### 4. Mock provider — `mock_provider.py`

The mock is not a test stub — it is a first-class adapter in the chain. It powers:

- **Offline execution**: `harness.cli demo` runs without API keys
- **Tests**: `tests/test_engine.py` uses it for full loop testing
- **Reproduction**: `harness.cli reproduce` replays recorded turns via replay mode

Two modes, selected by the constructor args passed:

```python
MockProvider(turns=[ScriptedTurn(...)], ...)  # scripted mode
MockProvider(replay=[{...}, {...}], ...)       # replay mode
```

**Scripted mode:** You pre-declare what the model will "say" on each turn. Each
`ScriptedTurn` has either `text` (the model finishes) or `tool_calls` (the model
requests tools):

```python
class ScriptedTurn(BaseModel):
    thinking: Optional[str] = None
    text: Optional[str] = None
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)
```

The mock advances through the list via an `itertools.count()` counter. Turn 0 is
the first call, turn 1 the second, etc.

**Replay mode:** Initialized with recorded `model_invocations.jsonl` entries
(the same dicts the `FileSink` writes). Rebuilds the neutral blocks from the stored
JSON and returns them in order. This makes reproduction bit-exact: the same blocks
the model originally returned are replayed, and the engine produces the same
outputs from them.

**Force tool handling:** When the engine forces a tool (`force_tool != None`), the
mock honours it regardless of what the scripted turn says:

```python
if force_tool:
    payload = {}
    if idx < len(self._turns) and self._turns[idx].tool_calls:
        payload = self._turns[idx].tool_calls[0].input
    elif idx < len(self._turns) and self._turns[idx].text:
        payload = _maybe_json(self._turns[idx].text)
    return ModelResponse(
        blocks=[ToolUseBlock(id=f"mock-{idx}", name=force_tool, input=payload)],
        stop_reason=StopReason.TOOL_USE, ...
    )
```

This lets you script task output as plain JSON text (`ScriptedTurn(text='{"field": 1}')`),
and the mock turns it into the forced tool call that the task path expects.

**Script exhaustion:** If the loop runs more turns than the script has entries, the
mock returns `default_text` (by default `'{"status": "mock-exhausted"}'`) with
`stop_reason=END`. This terminates the loop cleanly rather than raising an exception,
which makes tests that accidentally over-run still produce a decision record instead
of crashing.

---

## The factory: where providers get registered

The factory (`harness/core/factory.py`) is the only file that knows which concrete
adapters exist. `build_providers` registers them:

```python
def build_providers(mock_turns=None, mock_replay=None) -> dict[str, ModelProvider]:
    providers = {
        "mock": MockProvider(turns=mock_turns, replay=mock_replay),
    }
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from harness.providers.anthropic_provider import AnthropicProvider
            providers["anthropic"] = AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"])
        except Exception as e:
            print(f"[factory] anthropic provider unavailable: {e}")
    if os.environ.get("OPENAI_API_KEY"):
        ...
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        ...
    return providers
```

Three properties of this design:

1. **Lazy imports.** `AnthropicProvider` is only imported if the key is set. The
   `anthropic` SDK is imported lazily inside the adapter's `__init__`. Running
   without any keys (offline tests) requires none of the three SDKs to be installed.

2. **Key-gated registration.** If `ANTHROPIC_API_KEY` is absent, `"anthropic"` is
   not in the registry. When the chain reaches a link with `provider: anthropic`
   and no matching registry entry, it logs a warning and skips to the next link.
   No crash, no stub — the link is silently absent.

3. **Always-registered mock.** `"mock"` is always in the registry regardless of
   keys. A package with `- provider: mock` in its chain will always work. The test
   suite exploits this: it never touches real providers.

---

## The `ThinkingBlock` policy: record but never replay

Every adapter drops `ThinkingBlock` on the outbound translation:

```python
# All three adapters, in their message-to-wire-format functions:
# ThinkingBlock intentionally dropped on outbound
```

This is deliberate. Extended thinking (Claude) and chain-of-thought reasoning (o1)
produce private reasoning text. Replaying it to a *different* provider on the next
turn (or the next run) is:

- **Meaningless**: a different provider's model has no context for another model's
  reasoning
- **Expensive**: reasoning blocks can be large; sending them back inflates input tokens
- **Potentially wrong**: some providers may confuse replayed thinking with their own

The harness records `ThinkingBlock`s in the neutral decision log (`model_invocations.jsonl`)
for human auditability. It just never sends them back to any model. The engine loop
sees them in `response.blocks` (where they may be logged), but the next turn's
`messages.append(Message.assistant_blocks(response.blocks))` — wait, let me clarify
how this works:

In the engine, after each model response:
```python
messages.append(Message(role="assistant", content=response.blocks))
```

This appends all blocks including `ThinkingBlock`. But when the *next* turn calls
`provider.complete(messages=messages, ...)`, the adapter's message-to-wire
translation silently drops every `ThinkingBlock` it encounters. So thinking is in
the in-memory message list (for continuity within the Python process) but never
transmitted to any API.

This means `resume` also works cleanly: `Continuation.messages` serializes the full
message history including thinking blocks, but when the continuation is loaded and
played back through any adapter, the thinking blocks vanish at the translation layer.

---

## `ref.extra` — provider-specific pass-through

```python
params.update(ref.extra)  # at the bottom of each provider's complete()
```

`ref.extra` is a free-form dict declared in the package YAML:

```yaml
- provider: anthropic
  model: claude-opus-4-8
  extra:
    thinking:
      type: enabled
      budget_tokens: 5000
```

```yaml
- provider: openai
  model: o3
  extra:
    reasoning:
      effort: medium
```

It is passed through verbatim to the API call. No adapter validates it — the API
does. This avoids the factory needing to know about every provider capability;
new capabilities (streaming, extended context, response format) can be passed
via `extra` without touching the adapter code.

The design constraint: `extra` is merged last, so it can override other params the
adapter computed (like `temperature`). Use this carefully — `extra: {model: "other"}` 
would override the model string.

---

## Provider comparison table

| Feature | Anthropic | OpenAI | Gemini | Mock |
|---|---|---|---|---|
| SDK | `anthropic` | `openai` | `google-genai` | built-in |
| API | Messages API | Responses API | `generate_content` | scripted/replay |
| System prompt param | `system` | `instructions` | `system_instruction` in config | ignored |
| Tool declaration shape | flat list `{name, description, input_schema}` | `[{type:function, name, description, parameters}]` | `Tool(function_declarations=[...])` | N/A |
| Force tool | `tool_choice: {type:tool, name:...}` | `tool_choice: {type:function, name:...}` | `ToolConfig(mode=ANY, allowed_function_names=[...])` | forced `ToolUseBlock` |
| Tool result correlation | by `tool_use_id` | by `call_id` | by function `name` | N/A |
| Tool result wire | `{type:tool_result, ...}` inside `content` | `{type:function_call_output}` as top-level item | `Part(function_response=...)` | N/A |
| Image support | ✅ native base64 | ✅ `data:...;base64,...` URL | ✅ `Blob(data=bytes)` | N/A |
| PDF support | ✅ native | ❌ degraded to text | ✅ native | N/A |
| Thinking/reasoning | `ThinkingBlock` from `thinking` part | `ThinkingBlock` from `reasoning` items | `ThinkingBlock` from `thought=True` parts | scripted |
| Cache token fields | `cache_read`, `cache_write` | `cached_tokens` in `input_tokens_details` | `cached_content_token_count` | zeroes |
| Stop reason | `end_turn` / `tool_use` / `max_tokens` | inferred from output items | `candidates[0].finish_reason` | scriptable |
| Roles | `user` / `assistant` | `user` / `assistant` | `user` / `model` | N/A |

---

## Tracing through a real call

Here is the full journey of one model call in the underwriting demo:

```
engine._model_call(pkg, system, messages, tools, force_tool=None, depth=0)
  └── ModelChain.complete(chain=pkg.inference.chain, fallback_enabled=True)
        sorted by priority: [anthropic/claude-opus-4-8, openai/gpt-4.1, gemini/gemini-2.5-pro]
        link 0: anthropic
          attempt 0:
            AnthropicProvider.complete(ref=..., system=..., messages=..., tools=...)
              → _to_anthropic_msg(m) for each message in history
              → params = {model: "claude-opus-4-8", max_tokens: 2048, messages: [...],
                          system: ..., tools: [...], tool_choice: {type: auto}}
              → await self._client.messages.create(**params)
              ← resp.content = [ToolUseBlock(type="tool_use", id="toolu_01...",
                                             name="delegate_to_agent", input={...})]
              ← resp.stop_reason = "tool_use"
            → ModelResponse(blocks=[ToolUseBlock(...)], stop_reason=TOOL_USE, usage=...,
                            model="claude-opus-4-8", provider="anthropic")
        ← return ModelResponse
  ← return ModelResponse
```

The engine gets back a `ModelResponse` with neutral blocks. It never sees an
`anthropic.types.Message`, an `openai.types.responses.Response`, or any Gemini
type. The provider boundary is a hard wall.

---

## Translation at the boundary — what each adapter does

Each adapter's `complete` method is a pure translation function. Taking Anthropic
as an example, the full data transformation for one turn looks like this:

```
INBOUND — engine hands neutral IR to the adapter:

  Message(role="user", content=[
    TextBlock(text="Underwrite this..."),
    DocumentBlock(media_type="text/plain", data_b64="SGVsbG8...")
  ])

  ──► _to_anthropic_msg() ──►

  {"role": "user", "content": [
    {"type": "text", "text": "Underwrite this..."},
    {"type": "document", "source": {"type": "text",
     "media_type": "text/plain", "data": "Hello..."}}
  ]}

OUTBOUND — vendor response re-enters as neutral IR:

  resp.content = [
    AnthropicToolUseBlock(id="toolu_01", type="tool_use",
                          name="rate_property", input={...})
  ]

  ──► adapter parses ──►

  ModelResponse(
    blocks=[ToolUseBlock(id="toolu_01", name="rate_property", input={...})],
    stop_reason=StopReason.TOOL_USE,
    usage=Usage(input_tokens=812, output_tokens=47),
    model="claude-opus-4-8", provider="anthropic"
  )
```

The adapter is the only place where `anthropic.types.Message` or any
SDK-specific type appears. Once the adapter returns `ModelResponse`, the engine
works with neutral IR for the rest of the turn.

---

## Adding a new provider

Adding a provider (e.g., Bedrock, Ollama) requires:

1. **Write the adapter class** in `harness/providers/bedrock_provider.py`:
   - Implement `async def complete(self, *, ref, system, messages, tools, force_tool) -> ModelResponse`
   - Translate neutral messages to Bedrock's `InvokeModel` / `Converse` format
   - Map Bedrock exceptions to `RetryableProviderError` / `FatalProviderError`
   - Return a neutral `ModelResponse`

2. **Register it in the factory** (`factory.py`):
   ```python
   if os.environ.get("AWS_REGION"):
       from harness.providers.bedrock_provider import BedrockProvider
       providers["bedrock"] = BedrockProvider(region=os.environ["AWS_REGION"])
   ```

3. **Reference it in a package chain:**
   ```yaml
   - provider: bedrock
     model: anthropic.claude-3-5-sonnet-20241022-v2:0
     priority: 3
   ```

The engine, the loop, the tool gateway, the binder, and every other module are
unaffected.

---

## Checkpoint

1. `ModelChain` receives a `RetryableProviderError` from the primary link on
   attempt 2 (the last retry). What does it do next?

2. A `FatalProviderError` fires on link 0 (Anthropic) on attempt 0. How many more
   calls to Anthropic does the chain make?

3. A package has three links (Anthropic, OpenAI, Gemini) and `fallback_enabled: true`.
   The global `HARNESS_FALLBACK_ENABLED` env var is `false`. What is the effective
   chain length, and why?

4. The Gemini adapter pre-scans all messages to build `id_to_name`. Why can't it
   just look up the name in the current tool result block (`b.name`)?

5. A `DocumentBlock` with `media_type: "application/pdf"` is in the first user
   message. What happens when that message reaches the OpenAI adapter, and why?

6. `ThinkingBlock`s are stored in the in-memory `messages` list and serialized into
   the continuation store. But they are dropped by every adapter on outbound
   translation. Does this cause any correctness problem? Why or why not?

7. The mock's script is two turns but the loop runs three turns (perhaps because
   a tool result prompted one more model call). What does the mock return on
   turn 3, and does the loop crash?

When you can answer these, move to **[Module 06: The Tool Gateway](06-tool-gateway.md)**
— how the gateway authorizes tool calls, routes to the right transport
(python / MCP stdio / MCP HTTP / verity builtin), and how delegation re-enters
the engine at depth+1.