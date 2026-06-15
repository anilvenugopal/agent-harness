# Module 02 — How LLM Tool Use Actually Works

## The single-turn API call (baseline)

When you call Claude with no tools, the exchange is simple:

```
You send:
  {
    "model": "claude-opus-4-8",
    "messages": [
      { "role": "user", "content": "What is 2 + 2?" }
    ]
  }

Claude returns:
  {
    "content": [ { "type": "text", "text": "4" } ],
    "stop_reason": "end_turn"
  }
```

`stop_reason: "end_turn"` means: *I'm done, here is my answer.*

The loop reads this and exits. One round trip, done.

---

## What happens when you give the model tools

When you declare tools, you send them alongside your messages:

```
You send:
  {
    "model": "claude-opus-4-8",
    "messages": [
      { "role": "user", "content": "What is the risk score for applicant 42?" }
    ],
    "tools": [
      {
        "name": "calculate_risk_score",
        "description": "Compute a 0-100 risk score from applicant attributes.",
        "input_schema": {
          "type": "object",
          "properties": {
            "age":          { "type": "integer" },
            "risk_band":    { "type": "string"  },
            "prior_claims": { "type": "integer" }
          }
        }
      }
    ]
  }
```

The model reads the tool descriptions, decides it needs to use one, and instead
of answering, it returns a **tool call request**:

```
Claude returns:
  {
    "content": [
      {
        "type": "tool_use",
        "id":   "toolu_abc123",
        "name": "calculate_risk_score",
        "input": { "age": 34, "risk_band": "medium", "prior_claims": 2 }
      }
    ],
    "stop_reason": "tool_use"       ← NOT "end_turn"
  }
```

`stop_reason: "tool_use"` means: *I want you to run a tool. Don't give me the final answer yet — give me the tool result first, then I'll continue.*

The model has **not answered the question**. It has paused and is waiting for external information.

---

## The loop is mandatory because of this pause

You must now:
1. Execute the tool (`calculate_risk_score(age=34, ...)`)
2. Get the result (`{ "risk_score": 42.2 }`)
3. Send it **back** to the model in the same conversation
4. Ask the model to continue

```
You send (second request):
  {
    "messages": [
      { "role": "user",      "content": "What is the risk score for applicant 42?" },
      { "role": "assistant", "content": [ { "type": "tool_use", "id": "toolu_abc123", ... } ] },
      { "role": "user",      "content": [
          {
            "type":        "tool_result",
            "tool_use_id": "toolu_abc123",    ← must match the id above
            "content":     { "risk_score": 42.2 }
          }
        ]
      }
    ]
  }
```

Now the model has the tool result and can reason about it. It might:

- Return a final answer (`stop_reason: "end_turn"`) — loop exits
- Call another tool (`stop_reason: "tool_use"`) — loop continues

This is why the harness has a **loop**. You cannot do agentic work in a single HTTP call.

---

## The full picture as a sequence

```
┌────────┐           ┌────────────┐         ┌──────────┐
│  Loop  │           │ Claude API │         │   Tool   │
└───┬────┘           └─────┬──────┘         └────┬─────┘
    │                      │                     │
    │── Request (turn 0) ─►│                     │
    │   [user message +    │                     │
    │    tool definitions] │                     │
    │                      │                     │
    │◄─ stop_reason:  ─────│                     │
    │   "tool_use"         │                     │
    │   + ToolUseBlock     │                     │
    │                      │                     │
    │──────────────────────────── call tool ────►│
    │                      │                     │
    │◄──────────────────────────── result ───────│
    │                      │                     │
    │── Request (turn 1) ─►│                     │
    │   [history so far +  │                     │
    │    tool result]      │                     │
    │                      │                     │
    │◄─ stop_reason:  ─────│                     │
    │   "end_turn"         │                     │
    │   + TextBlock        │                     │
    │                      │                     │
  DONE                     │                     │
```

---

## The problem: Claude and Gemini speak different dialects

Both providers support tool use, but their wire formats differ.

**Claude** uses:
- Role `"assistant"` for model turns
- `"type": "tool_use"` blocks in content
- `"type": "tool_result"` blocks with `tool_use_id`
- Stop reason string: `"tool_use"`

**Gemini** uses:
- Role `"model"` for model turns (not `"assistant"`)
- `Part` with a `function_call` field
- `Part` with a `function_response` correlated by **function name** (not ID)
- Stop reason: a `FinishReason` enum, not a string

If the loop code read Claude's format directly, switching to Gemini would require rewriting the loop. That is unacceptable for a governance system that needs to be provider-agnostic.

---

## The solution: the Neutral IR

The harness defines its own message format — the **Intermediate Representation (IR)**.
Every provider adapter translates *in* to IR and *out* from IR. The loop only ever sees IR. It never imports `anthropic` or `google.genai`.

```
             ┌──────────────────────────────────────┐
             │          NEUTRAL IR                  │
             │                                      │
Claude API ──► AnthropicProvider ──► IR ──► Engine  │
             │                                      │
Gemini API ──► GeminiProvider    ──► IR ──► Engine  │
             │                                      │
  Mock    ──►  MockProvider      ──► IR ──► Engine  │
             └──────────────────────────────────────┘
```

Translation happens in **both directions**:
- **Inbound**: model response → IR (provider adapter does this)
- **Outbound**: IR messages → provider format (provider adapter does this)

---

## The IR types — what the loop actually reads

These live in `harness/core/ir.py`. Here are the five types:

### 1. `TextBlock` — plain text from the model
```python
class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
```
When the model writes prose ("The risk score is 42.2, which indicates..."),
it arrives as a `TextBlock`.

### 2. `ToolUseBlock` — the model's request to call a tool
```python
class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id:    str               # correlation id — must match the result
    name:  str               # which tool
    input: dict[str, Any]    # the arguments the model chose
```
`id` is critical. When you send the result back, you must reference this same id
so the model knows which call you're responding to.

### 3. `ToolResultBlock` — the result you send back
```python
class ToolResultBlock(BaseModel):
    type:        Literal["tool_result"] = "tool_result"
    tool_use_id: str     # matches the ToolUseBlock.id above
    content:     Any     # the result (dict, string, etc.)
    is_error:    bool    # True = tool failed; model decides how to recover
```
`is_error=True` does NOT abort the run. It tells the model "the tool failed" and
lets the model decide what to do — retry, ask for different inputs, or give up
gracefully. This is how denied HITL gates, MCP errors, and auth failures all flow
back into the loop without crashing it.

### 4. `Message` — one turn in the conversation
```python
class Message(BaseModel):
    role:    Literal["system", "user", "assistant", "tool"]
    content: list[ContentBlock]   # list of the blocks above
```
The full conversation history is a `list[Message]`. The loop appends to this
list every turn and sends the whole thing to the model each time (LLM APIs are stateless — they need the full history on every call).

### 5. `ModelResponse` — what every provider returns to the loop
```python
class ModelResponse(BaseModel):
    blocks:      list[ContentBlock]  # what the model said
    stop_reason: StopReason          # END, TOOL_USE, MAX_TOKENS, OTHER
    usage:       Usage               # tokens consumed
    model:       str                 # which model actually ran (for the audit log)
    provider:    str                 # "anthropic", "gemini", etc.
```

The loop only branches on **two things** from this:
```python
if response.stop_reason != StopReason.TOOL_USE:
    # Done — exit the loop
    break

for tc in response.tool_calls:   # response.tool_calls → list[ToolUseBlock]
    # Run each tool, collect results, continue
```

That's it. The loop is simple because the IR is doing the translation work.

---

## How `StopReason` works across providers

Every provider signals "I want a tool" differently. Each adapter normalizes to
the same three-value enum before the loop ever sees it.

**Claude** (`anthropic_provider.py` — a dict lookup on a raw string):

| Raw `stop_reason` string | Normalized to |
|---|---|
| `"end_turn"` or `"stop_sequence"` | `StopReason.END` |
| `"tool_use"` | `StopReason.TOOL_USE` |
| `"max_tokens"` | `StopReason.MAX_TOKENS` |
| `"pause_turn"` | `StopReason.OTHER` |

**Gemini** (`gemini_provider.py` — flag-based, not string-based):

| Condition on response | Normalized to |
|---|---|
| Any `function_call` part present in content | `StopReason.TOOL_USE` |
| No function_call; finish_reason doesn't end in `"MAX_TOKENS"` | `StopReason.END` |
| finish_reason string ends in `"MAX_TOKENS"` | `StopReason.MAX_TOKENS` |

Gemini doesn't return a clean `"tool_use"` string — the adapter inspects whether
any function_call blocks were present in the response content.

**OpenAI** (`openai_provider.py` — also flag-based, keyed on response status):

| Condition on response | Normalized to |
|---|---|
| `function_call` item present in `resp.output` | `StopReason.TOOL_USE` |
| No function_call; `status` is not `"incomplete"` | `StopReason.END` |
| `status == "incomplete"` and `reason == "max_output_tokens"` | `StopReason.MAX_TOKENS` |

The loop never sees any of these raw strings or conditions. It reads only the
normalized enum — one branch, three providers.

---

## Full IR translation reference

Every adapter must translate in **two directions**: inbound (provider response → IR)
and outbound (IR → provider request format). The tables below show the exact
mapping for each IR field, derived from the three provider files.

### Inbound — provider response → IR

| IR Type | IR Field | Claude | Gemini | OpenAI |
|---|---|---|---|---|
| `TextBlock` | `text` | `b.text` (block type `"text"`) | `p.text` (part with no `thought` flag) | `c.text` (content type `"output_text"`) |
| `ThinkingBlock` | `thinking` | `b.thinking` (block type `"thinking"`) | `p.text` where `p.thought == True` | summary text from `type == "reasoning"` item |
| `ToolUseBlock` | `id` | `b.id` | `fc.id` if present, else `"gemini-{fc.name}"` | `item.call_id` or `item.id` |
| `ToolUseBlock` | `name` | `b.name` | `fc.name` | `item.name` |
| `ToolUseBlock` | `input` | `dict(b.input)` | `dict(fc.args or {})` | `json.loads(item.arguments)` |
| `ModelResponse` | `stop_reason` | dict lookup on `resp.stop_reason` string | flag: `tool_seen` or `finish_reason` suffix | flag: `tool_seen` or `resp.status` |
| `ModelResponse` | `model` | `resp.model` | `ref.model` (Gemini doesn't echo it) | `resp.model` |
| `ModelResponse` | `request_id` | `resp.id` | `resp.response_id` | `resp.id` |
| `Usage` | `input_tokens` | `u.input_tokens` | `um.prompt_token_count` | `u.input_tokens` |
| `Usage` | `output_tokens` | `u.output_tokens` | `um.candidates_token_count` | `u.output_tokens` |
| `Usage` | `cache_read_tokens` | `u.cache_read_input_tokens` | `um.cached_content_token_count` | `u.input_tokens_details.cached_tokens` |
| `Usage` | `cache_write_tokens` | `u.cache_creation_input_tokens` | _(not exposed)_ | _(not exposed)_ |

### Outbound — IR → provider request format

| IR Type | IR Field | Claude | Gemini | OpenAI |
|---|---|---|---|---|
| `Message` | `role` | `"user"` / `"assistant"` | `"user"` / `"model"` | `"user"` / `"assistant"` |
| `TextBlock` | `text` | `{"type":"text","text":"..."}` | `Part(text="...")` | content string (joined) |
| `ToolUseBlock` | full block | `{"type":"tool_use","id":...,"name":...,"input":...}` | `Part(function_call=FunctionCall(name=...,args=...))` | `{"type":"function_call","call_id":...,"name":...,"arguments":json}` |
| `ToolResultBlock` | correlation key | by `tool_use_id` → `"tool_use_id"` field | by function **name** (not id — Gemini maps id → name via a lookup table) | by `tool_use_id` → `"call_id"` field |
| `ToolResultBlock` | content | `{"type":"tool_result","tool_use_id":...,"content":str,"is_error":bool}` | `Part(function_response=FunctionResponse(name=...,response=dict))` | `{"type":"function_call_output","call_id":...,"output":str}` |
| `ToolDef` | definition shape | `{"name":...,"description":...,"input_schema":...}` | `FunctionDeclaration(name=...,description=...,parameters=...)` | `{"type":"function","name":...,"description":...,"parameters":...}` |
| `ThinkingBlock` | on outbound | **dropped** (provider-private reasoning) | **dropped** | **dropped** |

**Gemini quirk — correlation by name, not id:**
Claude and OpenAI correlate tool results to tool calls using the `id` field.
Gemini (historically) correlates by function **name**. The Gemini adapter maintains
an `id → name` lookup map built from the outbound message history, so when it
encounters a `ToolResultBlock`, it can look up the correct name to send back.
This is transparent to the loop — it always uses `tool_use_id`.

---

## How the adapter translates — a concrete example

Here is the Anthropic adapter receiving a response and translating it to IR
(from `harness/providers/anthropic_provider.py`, simplified):

```python
# Raw Anthropic response content blocks:
for b in resp.content:
    if b.type == "text":
        blocks.append(TextBlock(text=b.text))
    elif b.type == "tool_use":
        blocks.append(ToolUseBlock(id=b.id, name=b.name, input=dict(b.input)))

# Raw Anthropic stop reason → IR enum:
_STOP = {
    "end_turn":   StopReason.END,
    "tool_use":   StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
}
stop_reason = _STOP.get(resp.stop_reason, StopReason.OTHER)

# Return the neutral ModelResponse:
return ModelResponse(
    blocks=blocks,
    stop_reason=stop_reason,
    usage=Usage(input_tokens=..., output_tokens=...),
    model=resp.model,
    provider="anthropic",
)
```

The Gemini adapter does the same thing, translating from `Part.function_call` → `ToolUseBlock`. The loop never needs to know which one ran.

---

## What the loop does with this

```python
# From harness/core/engine.py — the core of the loop:

response = await self.chain.complete(...)  # returns ModelResponse

if response.stop_reason != StopReason.TOOL_USE:
    status = RunStatus.COMPLETE
    break                                  # model is done

for tc in response.tool_calls:             # list[ToolUseBlock]
    rec = await self.tools.call(
        name=tc.name,
        tool_input=tc.input,
        ...
    )
    results.append(ToolResultBlock(
        tool_use_id=tc.id,                 # correlation id preserved
        content=rec["output_data"],
        is_error=rec["error"],
    ))

messages.append(Message.tool_results(results))  # append to history
# loop continues → next turn
```

Clean, readable, and completely provider-agnostic. `chain.complete()` could be
Claude or Gemini — the loop code is identical either way.

---

## Checkpoint

1. What does `stop_reason: "tool_use"` mean, and what must the loop do next?
2. Why does `ToolResultBlock` have a `tool_use_id` field?
3. What is `is_error=True` on a `ToolResultBlock`, and why doesn't it crash the run?
4. What is the job of a provider adapter, in one sentence?
5. Why does the loop send the **full message history** on every turn instead of
   just the latest message?

**Optional before continuing:**
- **[Appendix A01](appendix-01-agent-loop-basics.md)** — see what the raw agent loop
  looks like coded directly against Claude, Gemini, and OpenAI SDKs, with no harness.
  Good for anchoring exactly what the harness replaces.
- **[Appendix A02](appendix-02-async-python.md)** — if `async def` and `await` are
  unfamiliar, read this before Module 04. Module 03 does not require it.

When you can answer the checkpoint questions, move to
**[Module 03: Packages](03-packages.md)** — the YAML files that declare what an agent
does: which model, which tools, which sources and targets, and what the output must
look like. This is the configuration layer the engine reads before it runs anything.