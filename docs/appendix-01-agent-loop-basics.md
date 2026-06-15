# Appendix 01 — Agent Loop Basics: Raw Claude, Gemini, and OpenAI

This appendix shows what the agent loop looks like when coded **directly** against
each provider SDK — no harness, no abstraction. Its purpose is to make concrete
exactly what the harness replaces, and why the divergences across providers make
a neutral layer necessary.

---

## The minimal loop — Claude (Anthropic SDK)

```python
import anthropic

client = anthropic.Anthropic(api_key="...")

tools = [{
    "name": "calculate_risk_score",
    "description": "Compute a 0-100 risk score.",
    "input_schema": {
        "type": "object",
        "properties": {
            "age":          {"type": "integer"},
            "risk_band":    {"type": "string"},
            "prior_claims": {"type": "integer"},
        },
        "required": ["age", "risk_band", "prior_claims"],
    },
}]

messages = [{"role": "user", "content": "Score applicant: age=34, band=medium, claims=2"}]

while True:
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        tools=tools,
        messages=messages,
    )

    # Append the assistant's turn to history
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        # Model is done — extract text and exit
        text = next(b.text for b in response.content if b.type == "text")
        print("Answer:", text)
        break

    elif response.stop_reason == "tool_use":
        # Execute each requested tool, collect results
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = calculate_risk_score(**block.input)   # your function
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,                   # correlation by ID
                    "content":     str(result),
                })

        # Send results back as a user message and loop
        messages.append({"role": "user", "content": tool_results})
```

**What to notice:**
- The loop is manual — you write `while True` and manage the `messages` list yourself
- `stop_reason == "end_turn"` → done; `"tool_use"` → run tools, continue
- Tool results are correlated to calls by `tool_use_id` (matches `block.id`)
- The assistant's raw `response.content` (a list of typed blocks) goes back into messages as-is

---

## The same loop — Gemini (google-genai SDK)

```python
from google import genai
from google.genai import types

client = genai.Client(api_key="...")

tools = [types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="calculate_risk_score",
        description="Compute a 0-100 risk score.",
        parameters={
            "type": "object",
            "properties": {
                "age":          {"type": "integer"},
                "risk_band":    {"type": "string"},
                "prior_claims": {"type": "integer"},
            },
        },
    )
])]

contents = [types.Content(
    role="user",
    parts=[types.Part(text="Score applicant: age=34, band=medium, claims=2")]
)]

while True:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(tools=tools),
    )

    cand    = response.candidates[0]
    parts   = cand.content.parts
    has_fn  = any(getattr(p, "function_call", None) for p in parts)

    # Append model turn to history
    contents.append(types.Content(role="model", parts=parts))   # "model", not "assistant"

    if not has_fn:
        text = next(p.text for p in parts if getattr(p, "text", None))
        print("Answer:", text)
        break

    # Execute tools, collect function responses
    result_parts = []
    for p in parts:
        fc = getattr(p, "function_call", None)
        if fc:
            result = calculate_risk_score(**fc.args)
            result_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fc.name,                              # correlation by NAME, not ID
                    response={"result": result},
                )
            ))

    # Send results back as a user message and loop
    contents.append(types.Content(role="user", parts=result_parts))
```

**What diverges from Claude:**
| Aspect | Claude | Gemini |
|---|---|---|
| Model role label | `"assistant"` | `"model"` |
| Stop signal | `stop_reason == "tool_use"` string | inspect parts: any `function_call` present? |
| Tool definition | `input_schema` (JSON Schema dict) | `FunctionDeclaration` with `parameters` |
| Tool call object | `block.id`, `block.name`, `block.input` | `fc.id` (optional), `fc.name`, `fc.args` |
| Result correlation | by `tool_use_id` (the call's `id`) | by function `name` (historically no stable id) |
| Result format | `{"type":"tool_result","tool_use_id":...}` | `FunctionResponse(name=..., response=...)` |
| Result sent as | user message with list of tool_result dicts | user `Content` with `function_response` parts |

---

## The same loop — OpenAI (Responses API)

```python
from openai import OpenAI
import json

client = OpenAI(api_key="...")

tools = [{
    "type":        "function",
    "name":        "calculate_risk_score",
    "description": "Compute a 0-100 risk score.",
    "parameters": {
        "type": "object",
        "properties": {
            "age":          {"type": "integer"},
            "risk_band":    {"type": "string"},
            "prior_claims": {"type": "integer"},
        },
    },
}]

input_items = [{"role": "user", "content": "Score applicant: age=34, band=medium, claims=2"}]

while True:
    response = client.responses.create(
        model="gpt-5.1",
        input=input_items,           # "input", not "messages"
        tools=tools,
    )

    tool_calls = [item for item in response.output if item.type == "function_call"]
    text_items = [item for item in response.output if item.type == "message"]

    if not tool_calls:
        text = text_items[0].content[0].text if text_items else ""
        print("Answer:", text)
        break

    # Append assistant's output to history
    for item in response.output:
        input_items.append(item)

    # Execute tools, send results back
    for tc in tool_calls:
        args   = json.loads(tc.arguments)
        result = calculate_risk_score(**args)
        input_items.append({
            "type":    "function_call_output",
            "call_id": tc.call_id,              # correlation by call_id (like Claude's id)
            "output":  json.dumps(result),       # must be a string
        })
```

**What diverges from Claude and Gemini:**
| Aspect | Claude | Gemini | OpenAI |
|---|---|---|---|
| Conversation param | `messages` | `contents` | `input` |
| History append | assistant turn as message dict | `Content(role="model")` | raw output items |
| Stop signal | `stop_reason` string | inspect parts | inspect output item types |
| Tool result type | `"tool_result"` | `function_response` part | `"function_call_output"` |
| Result correlation | `tool_use_id` | function `name` | `call_id` |
| Result content | string or dict | dict | **string only** (must JSON-encode dicts) |
| System prompt | `system` param | `system_instruction` in config | `instructions` param |

---

## What the harness replaces

Each loop above is ~30–40 lines of provider-specific code that you would have to
write and maintain for every agent in your system. Multiply by three providers,
and every divergence above becomes a bug surface.

The harness collapses all three into:

```python
response = await self.chain.complete(
    chain=pkg.inference.chain,
    system=system,
    messages=messages,       # list[Message] — neutral IR
    tools=tool_defs,
    force_tool=force_tool,
)
# response is always ModelResponse — same shape, same fields, regardless of provider
```

The adapter (Anthropic/Gemini/OpenAI provider file) absorbs every divergence in
the tables above. The loop reads only `response.stop_reason`, `response.tool_calls`,
and `response.text`. That's it.

---

## The async version (how the harness actually calls it)

The loops above are synchronous — they block while waiting for the network.
The harness uses `async/await` so it can handle many concurrent runs without
blocking. The call becomes:

```python
# Synchronous (blocks the thread):
response = client.messages.create(...)

# Async (yields control while waiting for the network):
response = await client.messages.create(...)
```

The shape is identical — `await` just means "start this network call, give up the
thread until it completes, then come back here." See **[Appendix A02](appendix-02-async-python.md)**
for a full explanation of how this works.
