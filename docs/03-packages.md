# Module 03 — Packages: Declaring What an Agent Does

## What a package is

A **package** is a YAML file that fully describes one agent or task. It is the
configuration layer — it tells the engine *what* to do, not *how* to do it. The
engine reads the package, then executes it.

```
packages/classify_document.task.yaml       → a Task
packages/underwriting_agent.agent.yaml     → an Agent
packages/loss_history_analyst.agent.yaml   → an Agent (delegation target)
```

Every field in these files maps to a Python class in `harness/core/package.py`.
The relationship is exact — YAML key names match Python field names. When you
call `Package.load("path/to.yaml")`, Pydantic validates every field and gives you
a typed Python object:

```python
# harness/core/package.py
@classmethod
def load(cls, path: str | Path) -> "Package":
    data = yaml.safe_load(Path(path).read_text())
    return cls.model_validate(data)     # Pydantic validates every field
```

If a required field is missing or the wrong type, you get a clear error at load
time — not mid-run.

---

## The full Package structure

```
Package
├── name              string          "underwriting_agent"
├── kind              task | agent
├── version           string          "v1"
├── description       string
├── system_prompt     string          the role/persona given to the model
├── prompt_template   string          the opening user message ({{input.x}} substitution)
│
├── inference         InferenceConfig
│   ├── chain         list[ModelRef]  ordered fallback chain
│   ├── max_turns     int             hard ceiling on loop iterations
│   ├── max_total_tokens  int?        token budget for the whole run
│   └── max_usd       float?          cost ceiling in USD
│
├── tools             list[ToolAuthorization]
├── sources           list[SourceBinding]
├── targets           list[TargetBinding]
├── delegations       list[DelegationAuthorization]
├── hitl              HITLPolicy
└── output_schema     dict?           JSON Schema properties for structured output
```

---

## Field-by-field walkthrough

### `name` and `kind`

```yaml
name: underwriting_agent
kind: agent          # or: task
```

`name` is the key the engine uses to look up the package — it's what you pass to
`run_agent("underwriting_agent", ...)`. It must match the filename by convention
but is not enforced by the loader.

`kind` determines the execution path:
- **`task`** → `engine.run_task()` — one model turn, forced structured output, no loop
- **`agent`** → `engine.run_agent()` — multi-turn loop with tools, delegation, HITL

---

### `system_prompt` and `prompt_template`

```yaml
system_prompt: |
  You are a commercial property underwriting agent. You will be given a submission
  record for a building to be insured. Steps: (1) delegate to loss_history_analyst
  for loss history, (2) call lookup_appetite for carrier rules, (3) call
  property_data for third-party enrichment, (4) call rate_property (COPE rating),
  (5) if all checks pass call bind_policy (human approval required), then call
  submit_output. If any check fails, call submit_output directly with decision=refer.

prompt_template: |
  Underwrite this commercial property submission:
  submission_id: {{input.submission_id}}

  Submission record from system of record:
  {{context.submission}}
```

`system_prompt` sets the model's role. It goes into the `system` parameter of
every API call.

`prompt_template` becomes the first user message. The engine renders it using
two scopes:

| Token | Value |
|---|---|
| `{{input.x}}` | fields from the run's input dict |
| `{{context.x}}` | data fetched from sources (Postgres, etc.) |

So `{{context.submission}}` is populated by whatever the `pg_main` source
connector fetched before the loop starts. The model sees the rendered text — it
never knows about Postgres or the template engine.

---

### `inference` — the model chain

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
  max_turns: 10
  max_usd: 2.00
```

Each item in `chain` is a `ModelRef` — one link in the fallback chain. The engine
tries them in `priority` order (lowest number first). A link is skipped if its
provider has no API key registered; it is fallen back from if the provider returns
a retryable error (rate limit, server error) after exhausting retries.

**Per-link knobs on `ModelRef`:**

| Field | Purpose |
|---|---|
| `provider` | Which adapter to use: `anthropic`, `openai`, `gemini`, `mock` |
| `model` | The provider's model string, passed verbatim to the API |
| `priority` | Order: 0 = try first, 1 = first fallback, etc. |
| `max_tokens` | Maximum output tokens for this call |
| `temperature` | Optional; `null` = provider default |
| `top_p` | Optional; `null` = provider default |
| `extra` | Pass-through dict for provider-specific params (e.g. extended thinking config) |

**Run-level ceilings on `InferenceConfig`:**

| Field | What happens when exceeded |
|---|---|
| `max_turns` | Loop exits with status `max_turns` |
| `max_total_tokens` | `QuotaExceeded` raised; loop exits |
| `max_usd` | `QuotaExceeded` raised; loop exits |

These are hard ceilings, not soft warnings. The `QuotaEnforcer` checks them before
every turn.

---

### `tools` — what the model is allowed to call

```yaml
tools:
  - name: rate_property
    description: Compute annual premium via the COPE framework.
    transport: python_inprocess
    input_schema:
      type: object
      properties:
        tiv:              {type: number}
        occupancy:        {type: string}
        construction:     {type: string}
        protection_class: {type: integer}
        sprinklered:      {type: boolean}
        deductible:       {type: integer}
      required: [tiv, occupancy, construction, protection_class, sprinklered, deductible]

  - name: lookup_appetite
    description: Check carrier appetite and binding authority for a risk.
    transport: mcp_http
    mcp_server: policy_service
    mcp_tool_name: lookup_appetite
    input_schema:
      type: object
      properties:
        line:         {type: string}
        occupancy:    {type: string}
        construction: {type: string}
        state:        {type: string}
      required: [line, occupancy, construction, state]

  - name: delegate_to_agent
    description: Delegate a task to a registered sub-agent.
    transport: verity_builtin
    input_schema:
      type: object
      properties:
        agent_name: {type: string}
        context:    {type: object}
      required: [agent_name, context]

  - name: bind_policy
    description: Issue a binder. HUMAN APPROVAL REQUIRED.
    transport: python_inprocess
    input_schema:
      type: object
      properties:
        submission_id: {type: integer}
        premium:       {type: number}
        limit:         {type: number}
        deductible:    {type: integer}
      required: [submission_id, premium, limit, deductible]
```

Each entry is a `ToolAuthorization`. There are four transports:

```
transport: python_inprocess   → a Python function registered in the worker
                                (rate_property, bind_policy)

transport: mcp_http           → a remote MCP server over Streamable HTTP
transport: mcp_stdio          → an MCP server launched as a subprocess

transport: verity_builtin     → a built-in engine capability
                                (only delegate_to_agent today)
```

**Authorization is enforced.** Before any tool executes, the Tool Gateway checks
the requested tool name against this list. If the model tries to call a tool not
listed here — no matter how well-formed the request — it is denied and the model
gets an error result. The model never has more capability than the package grants.

The `input_schema` is also sent to the model as its tool description. The model
uses it to know what arguments to provide.

---

### `sources` — data pulled in before the loop

**Text source (Postgres row → prompt template):**
```yaml
sources:
  - connector: pg_main
    method: query
    ref: "SELECT * FROM submission WHERE id = {{input.submission_id}}"
    bind_to: submission
    required: true
```

Sources are resolved **before** the first model turn. The binder:
1. Templates `ref` with `{{input.submission_id}}` from the run's input
2. Calls the `pg_main` connector's `fetch("query", rendered_ref)` method
3. Places the result under `context["submission"]`
4. The prompt template then renders `{{context.submission}}` into the user message

**Binary source (S3 object → model content block):**
```yaml
sources:
  - connector: s3_main
    method: get_bytes
    ref: "{{input.document_key}}"
    bind_to: document
    as_block: document        # "image" | "document"
    required: true
```

When `as_block` is set, the connector returns raw bytes instead of text. The
binder wraps them in a block-metadata dict; the engine appends an `ImageBlock`
or `DocumentBlock` to the first user message rather than inserting text into
the prompt template. Provider adapters translate these to native wire formats:

| Provider | image | document (PDF) | document (text/*) |
|---|---|---|---|
| Anthropic | `{type:image, source:{type:base64}}` | `{type:document, source:{type:base64}}` | `{type:document, source:{type:text}}` |
| OpenAI | `{type:input_image, image_url:data:...}` | not supported (degraded to text) | text prepended to message |
| Gemini | `Part(inline_data=Blob(...))` | `Part(inline_data=Blob(...))` | `Part(inline_data=Blob(...))` |

`ImageBlock` and `DocumentBlock` are stored in the IR as base64 strings (not
raw bytes) so they round-trip through the decision log and HITL continuation
store without custom JSON encoders.

If `required: true` and the source fails (connector missing, object not found), the
run fails immediately with a `SourceResolutionError` — no model call is made.

If `required: false`, the failure is logged in the decision record as an audit
entry, and the loop continues with whatever context was resolved.

---

### `targets` — data written after the loop

```yaml
targets:
  - connector: s3_main
    method: put_object
    from_path: "$"
    container: "underwriting/{{run_id}}.json"
    required: false
```

Targets are written **after** the loop produces its final output. The binder:
1. Selects the output value using `from_path` (`"$"` = the whole output dict)
2. Templates `container` with `{{run_id}}`
3. Calls `s3_main.write("put_object", "underwriting/<run_id>.json", output_dict)`

`from_path` is a simple JSONPath-ish selector: `"$"` = whole output, `"$.field"` =
one field, `"$.a.b"` = nested. It lets you write different parts of the output to
different connectors.

`required: false` means a write failure is recorded in the audit log but does not
fail the run. The run status shows `complete`.

**This is the right place to be careful.** If a downstream process reads from S3
to consume the result, it will find nothing — and the run still says `complete`.
The decision log captures the output, but a consumer that reads from S3 (not from
the decision log) has experienced silent data loss.

The `required: true / false` flag:
- Set `required: false` only for genuinely optional secondary artifacts (a backup
  archive, a secondary notification) where no downstream process depends on it
- Set `required: true` for any target a downstream consumer actually reads

The example YAML files use `required: false` so that they could work for testing
without S3 configured. In a real production deployment, any target a consumer
depends on must be `required: true`, so the run fails loudly rather than
completing silently with missing data.

---

### `delegations` — which sub-agents this agent may spawn

```yaml
delegations:
  - child_agent: loss_history_analyst
```

This is a governance allowlist. When the model calls `delegate_to_agent` with
`agent_name: "loss_history_analyst"`, the engine checks this list first. If the name
is not here, the call is denied — the model cannot delegate to an agent the package
didn't authorize, even if that agent package exists.

---

### `hitl` — which tools require human approval

```yaml
hitl:
  enabled: true
  require_approval_for: [bind_policy]
```

When `enabled: true`, the engine checks every tool call against
`require_approval_for` before executing. If `bind_policy` is requested, the run
suspends (see Module 01 / Appendix HITL) until a human records a decision.

`enabled: false` (or the field absent) means no gates fire — all tools run
immediately. `loss_history_analyst` has no `hitl` field at all, which defaults to
`enabled: false`.

---

### `output_schema` — enforcing structured output

```yaml
output_schema:
  decision:
    type: string
    enum: [bound, quote, refer, decline]
  premium:
    type: number
  coverage_summary:
    type: object
  cope_factors:
    type: object
  referral_reasons:
    type: array
    items: {type: string}
  rationale:
    type: string
```

When present, the engine creates a synthetic tool (`submit_output` for agents,
`structured_output` for tasks) whose `input_schema` is this dict. The model is
forced to call that tool instead of returning free text. The tool's input
*becomes* the run's output — guaranteed to match the schema.

For **tasks** the engine uses `force_tool="structured_output"` (the provider's
tool_choice API). For **agents** the engine adds `submit_output` to the tool list
and exits the loop when the model calls it.

---

## The three packages compared

| Field | `classify_document` (task) | `underwriting_agent` (agent) | `loss_history_analyst` (agent) |
|---|---|---|---|
| `kind` | task | agent | agent |
| Model chain | Claude → Gemini | Claude → OpenAI → Gemini | Haiku → Gemini Flash |
| `fallback_enabled` | true | true | true |
| `max_turns` | 1 (task: forced) | 10 | 5 |
| `max_usd` | $0.50 | $2.00 | $0.25 |
| Tools | none | 5 (rate, appetite, property, delegate, binder) | 1 (pull_loss_runs via MCP) |
| Sources | S3 (`as_block: document`) | Postgres (submission row) | none |
| Targets | S3 (optional) | S3 (optional) | none |
| Delegations | none | loss_history_analyst | none |
| HITL | none | bind_policy | none |
| `output_schema` | yes (3 fields) | yes (6 fields) | yes (5 fields) |

Notice that `loss_history_analyst` is deliberately minimal — no sources, no targets,
no HITL, one tool. It is a narrow capability scoped to one job: pull and summarize
claims history. Governance principle: packages should declare *only* what they need.

---

## How the engine uses a package (preview of Module 04)

```python
# factory.py — at startup, all packages are loaded into a dict:
packages = load_packages("packages/")
# { "classify_document": Package(...),
#   "underwriting_agent": Package(...),
#   "loss_history_analyst": Package(...) }

# engine.py — at run time:
pkg = self.packages["underwriting_agent"]

# System prompt → goes straight to the model
system = pkg.system_prompt

# Sources → resolved before the loop
context, audit = await self.binder.resolve_sources(pkg.sources, input_data, mock)

# Tools → sent to the model as its tool menu
tool_defs = [ToolDef(name=t.name, description=t.description,
                     input_schema=t.input_schema) for t in pkg.tools]

# Inference chain → used to pick which model to call
response = await self.chain.complete(chain=pkg.inference.chain, ...)

# HITL → checked on every tool call
if self.tools.needs_approval(tc.name, pkg.hitl): ...

# Targets → written after the loop
await self.binder.write_targets(pkg.targets, output, ...)
```

The package is read-only during execution. Nothing in the loop mutates it. The
same package object can be shared across concurrent runs.

---

## Checkpoint

1. What is the difference between `sources` and `targets` — when does each execute?
2. A model requests a tool called `send_email`. The package has no `send_email`
   in its `tools` list. What happens?
3. What does `output_schema` do differently for a **task** vs an **agent**?
4. What is the real consequence of a target write failure with `required: false`, and why should you set `required: true` on a target?
5. What is the purpose of the `delegations` list — why not just let any agent
   delegate to any other?

When you can answer these, move to **[Module 04: The Execution Engine](04-execution-engine.md)**
— the loop itself: how `run_task` and `run_agent` use the package to drive turns,
dispatch tools, enforce quota, and produce the final result.