# Execution Engine — Strategic Design

> A multi-provider (Claude / OpenAI / Gemini) agent harness compatible with the
> Verity governance platform. This document describes the architecture; it is
> the design artifact for the runnable engine in this repository.

---

## 1. The one idea

**Every external effect goes through a gateway, and every gateway has a
mock/suppress seam.** A run only ever touches the outside world in four ways —
it calls a model, it calls a tool, it reads a source, it writes a target — so
there are exactly four gateways:

| Gateway | Effect | Mock seam | Suppress seam |
|---|---|---|---|
| **Model** | LLM inference | replay recorded turns | — |
| **Tool** | tool / MCP call | canned tool responses | (auth-deny is a kind of suppress) |
| **Source** | read an input | canned source values | — |
| **Target** | write an output | replay a write handle | shadow/challenger no-op |

Around these four gateways runs a **provider-agnostic agentic loop**. Orthogonal
to them sit the cross-cutting concerns: the decision-log assembler, the quota
enforcer, the HITL suspend point, and delegation recursion.

Two payoffs fall out of this single idea:

1. **Provider independence.** The loop reads only a neutral representation, so
   Claude, OpenAI, Gemini, the mock, and the replay provider are
   interchangeable behind one interface. A fallback chain across providers is
   just the loop calling the model gateway, which tries links in priority order.

2. **Audit reproduction is free.** "Reproduce decision X" is not a bespoke
   feature — it is *all four gateways in playback mode at once*. Input mock
   injection, tool/MCP mocking, and write suppression are the same capability
   applied to three different gateways.

---

## 2. Component map

```
                         ┌───────────────────────────────────────────────┐
   Package (.yaml)  ───▶ │                 ExecutionEngine                │
   (the governed unit)   │                                               │
                         │   run_task() · run_agent() · resume()         │
                         │                    │                          │
                         │            ┌────────▼────────┐                 │
                         │            │  agentic loop   │  (neutral IR)   │
                         │            └────────┬────────┘                 │
                         │   ┌─────────────────┼─────────────────┐        │
                         └───┼────────┬────────┬────────┬────────┼────────┘
                             ▼        ▼        ▼        ▼        ▼
                        ┌────────┐┌────────┐┌────────┐┌────────┐┌──────────┐
                        │ MODEL  ││  TOOL  ││ SOURCE ││ TARGET ││ DECISION │
                        │gateway ││gateway ││ binder ││ binder ││ assembler│
                        └───┬────┘└───┬────┘└───┬────┘└───┬────┘└────┬─────┘
            ┌───────────────┤         │         │         │          │
            ▼      ▼      ▼ │     ┌───┴───┐ ┌───┴───┐ ┌───┴───┐  ┌───▼────┐
        ┌──────┐┌──────┐┌──┐│     │ auth  │ │  PG   │ │  S3   │  │ File / │
        │claude││openai││..││     │enforce│ │connect│ │connect│  │Postgres│
        └──────┘└──────┘└──┘│     └───┬───┘ └───────┘ └───────┘  │  sink  │
         (provider adapters)│   ┌─────┴─────┬──────────┐         └────────┘
                            │   ▼           ▼          ▼
                       ┌─────────┐   ┌──────────┐ ┌───────────┐
                       │ python  │   │   MCP    │ │ verity     │
                       │ tools   │   │ stdio /  │ │ builtin    │
                       │         │   │ HTTP     │ │(delegate)  │
                       └─────────┘   └──────────┘ └───────────┘
```

Cross-cutting (not effects, but policy applied inside the loop): **quota
enforcer** (before each model call), **HITL gate** (before a gated tool runs),
**delegation** (a builtin tool that re-enters the loop), **tracer** (a passive
observer that turns every stage into a structured event).

| Component | File | Responsibility |
|---|---|---|
| Neutral IR | `core/ir.py` | `Message`, content blocks, `ToolDef`, `ModelResponse`, `Usage`, `StopReason` |
| Package | `core/package.py` | the unsigned YAML analog of `.vax`/`.vtx` |
| Engine | `core/engine.py` | the loop, task path, delegation, HITL suspend/resume |
| Model chain | `providers/base.py` | priority fallback + retry-with-jitter |
| Provider adapters | `providers/{anthropic,openai,gemini,mock}_provider.py` | IR ↔ vendor |
| Tool gateway | `tools/gateway.py` | auth enforcer → mock seam → transport routing |
| Connectors | `connectors/{base,postgres,s3}.py` | Category B data access |
| Binder | `connectors/binder.py` | source resolution + target writes + suppression |
| MCP client | `mcp/client.py` | stdio + Streamable HTTP, normalized results |
| HITL | `hitl/continuation.py` | durable suspend/resume checkpoint |
| Quota | `quota/enforcer.py` | per-run turn/token/cost ceilings |
| Decisions | `decisions/assembler.py` | record assembly + File/Postgres sinks |
| Mock context | `mock/context.py` | one object that flips all four gateways |
| Tracer | `core/trace.py` | Rich step-debuggable event stream |
| Worker | `worker/worker.py` | Postgres SKIP LOCKED claim loop |

---

## 3. The neutral IR (design decision D1)

The loop reads only `harness/core/ir.py`. A model turn is a `ModelResponse`
with four facts the loop branches on: its `text`, its `tool_calls`, its
`stop_reason` (a normalized enum, never a vendor string), and its `usage`.

Why not normalize to Anthropic's shape, or use a library like LiteLLM? Because
per-call audit fidelity *is* the product. A general abstraction hides exactly
the things a governance system needs — each provider's token accounting, cache
hits, per-call ids, reasoning traces. The neutral IR keeps those first-class and
keeps every vendor SDK out of the loop. The cost is three translation layers
(the adapters); the benefit is that adding a provider or a fallback never
touches the loop.

Messages carry **typed content blocks** rather than flat strings, because all
three providers return structured content and a flat string loses the tool
correlation that replay depends on:

| Block | Direction | Purpose |
|---|---|---|
| `TextBlock` | both | model text or user text |
| `ThinkingBlock` | inbound only | extended reasoning; dropped on outbound (provider-private) |
| `ToolUseBlock` | inbound | model's tool call request |
| `ToolResultBlock` | outbound | result of executing a tool |
| `ImageBlock` | outbound (first user msg) | inline image from a binary source binding |
| `DocumentBlock` | outbound (first user msg) | PDF or text document from a binary source binding |

`ImageBlock` and `DocumentBlock` carry their bytes as base64 strings (`data_b64`)
so they are JSON-serialisable and round-trip through the decision log and HITL
continuation store without custom encoders. Provider adapters translate them to
each vendor's wire format (Anthropic document/image source, OpenAI input_image,
Gemini inline_data Blob).

---

## 4. The loop as a state machine

`run_agent` and `resume` share one `_agent_loop`. Each iteration is a fixed
sequence of stages, every one of which emits a trace event:

```
            ┌─────────────────────────────────────────────┐
            │            quota.check_turn()                │  QUOTA_CHECK
            └───────────────────┬─────────────────────────┘
                                ▼
            ┌─────────────────────────────────────────────┐
   ┌───────▶│   model gateway (chain: try priority 0..N)   │  MODEL_ATTEMPT/…
   │        └───────────────────┬─────────────────────────┘  MODEL_RESPONDED
   │                            ▼
   │              stop_reason == TOOL_USE ?
   │              ├── no  ─────────────────────────────────▶  COMPLETE
   │              └── yes
   │                            ▼
   │              submit_output requested ? ───────────────▶  COMPLETE
   │                            ▼
   │              any gated tool ?
   │              ├── yes ─▶ save Continuation ────────────▶  SUSPENDED (raise)
   │              └── no
   │                            ▼
   │        ┌─────────────────────────────────────────────┐
   │        │  for each tool: auth → mock → dispatch       │  TOOL_CALLED/RESULT
   │        │   (python | MCP | delegate_to_agent)         │  DELEGATION_*
   │        └───────────────────┬─────────────────────────┘
   │                            ▼
   └──────────────── append tool_results, turn += 1
```

Terminal states: `COMPLETE`, `FAILED`, `SUSPENDED` (non-terminal in the
lifecycle sense — it resumes), `MAX_TURNS` (loop hit its turn budget still
wanting tools).

### Tasks vs agents

- **`run_task`** is the degenerate single-turn case: resolve sources → one model
  call forcing a synthetic `structured_output` tool (when a schema is declared)
  → write targets → log. No loop.
- **`run_agent`** is the full loop above, optionally with `submit_output`
  enforcement (a synthetic tool whose call ends the run and whose input is the
  answer).

---

## 5. Suspend / resume (design decision D6 — built fully)

The loop is a resumable state machine, and a HITL gate is just one suspend
point. The mechanism:

```
  loop hits gated tool
        │
        ▼
  serialise Continuation  ──────────────▶  durable store (file / pg)
   { messages (neutral), turn, usage,        a suspended run is just a row;
     tool_calls/source audit so far,         no worker / connection / memory
     pending_tool_use, mock context }        is held while the human thinks
        │
        ▼
  raise HITLSuspended  ───▶  worker marks run 'suspended', RELEASES the worker
        ⋮
   (human decides, asynchronously, out of band)
        ⋮
  record_decision(HumanDecision)  ──▶  flip run 'runnable'
        │
        ▼
  worker re-claims  ──▶  engine.resume(suspension_id)
        │
        ▼
  rebuild messages + assembler from checkpoint
  apply decision to the gated tool  (approve→run | edit→run-with/replace | deny→error)
  inject as tool_result  ──▶  continue _agent_loop at turn+1
```

Persisting the **neutral `messages`** (not the human-facing audit history) is
the key: those messages *are* what the model gateway replays into on the next
turn. The mock context is persisted too, so a suspended mocked/test run resumes
with the same suppression behaviour (and so replay stays deterministic).

**Scope: top-level agents only.** Sub-agent HITL is intentionally out of scope —
a synchronous `delegate()` would block the parent worker for human-time, which
is the exact anti-pattern suspend/resume exists to avoid. The engine *rejects*
a gate fired at `depth > 0`, returning a clean error to the sub-agent rather
than suspending. Async delegation would be the way to lift this, and is noted as
future work.

---

## 6. Multi-provider fallback (the model chain)

A package's `inference.chain` is a **priority-ordered list of `ModelRef`s**,
each naming a provider and that provider's model. `ModelChain.complete`:

1. tries priority 0 with up to `max_retries` attempts;
2. on a **retryable** error (429/5xx/timeout) backs off with **full jitter**
   and retries within the link;
3. on **exhausted retries** or a **fatal** error (auth/bad-request) **falls
   through** to priority 1, then 2, …;
4. raises only when every link is exhausted.

This is one mechanism serving two needs: resilience (retry the same model) and
heterogeneous fallback (Claude → OpenAI → Gemini). Each adapter is responsible
for mapping its vendor's exceptions to `RetryableProviderError` /
`FatalProviderError` so the chain can decide without knowing vendor specifics.

Full jitter (`sleep ~ U(0, base·2^attempt)`) is deliberate: lockstep backoff
makes N workers that all hit a 429 re-collide — the thundering-herd bug.

---

## 7. Tool routing (ADR-0016's three categories)

Every model-requested tool passes through `ToolGateway.call` in this order:

1. **Authorization enforcer (non-bypassable).** Is the tool in the package's
   declared `tool_authorizations`? If not, it is **never executed** — an error
   tool_result goes back to the model. This is the governance gate that stops a
   prompt-injected `tool_use` from reaching an un-authorized capability.
2. **HITL gate** (enforced in the loop, which owns the continuation store).
3. **Mock seam.** A `MockContext` response, or `mock_all_tools`, or the
   package's per-tool `mock_response` default — short-circuit without
   dispatching.
4. **Dispatch by transport:** `python_inprocess` (Category B-ish in-process
   callable) · `mcp_stdio` / `mcp_http` (Category A application MCP servers) ·
   `verity_builtin` (`delegate_to_agent`).

Every path returns the **same `tool_record` dict**, so the decision log records
all tool calls identically and the loop wraps the result into a neutral
`ToolResultBlock` with no transport-specific branch.

---

## 8. Connectors and binding

`SourceBinding` and `TargetBinding` in the package declare what the run reads
and writes; the **Binder** executes them over the connector registry.

- **Sources:** template the ref with the run input (`{{input.x}}`), fetch via
  the named connector, bind under a context key. Mock seam:
  `source_responses[bind_to]`.
- **Targets:** select the value from the output (`$.field`), template the
  container, write via the connector. Suppress seam: `suppress_targets`
  (shadow/challenger no-op that still records an audit entry). Replay seam:
  `target_handles`.

Connectors (`PostgresConnector`, `S3Connector`) import their drivers lazily, so
the offline path needs neither `psycopg` nor `boto3`. Credentials follow ADR-0010
Model B: the value lives in the environment; the engine knows only the connector
*name*.

---

## 9. Decision logging (design decision D4)

The **assembler** accumulates a `DecisionRecord` (the v1 31-column shape: flat
scalars + nested JSON) throughout a run. The **sink** persists it:

- **FileSink (default)** writes ADR-0015's per-run artifact layout —
  `runs/{yyyy}/{mm}/{dd}/{run_id}/decision_log.json` +
  `model_invocations.jsonl`. Zero infra; the exact shape the Verity hub ingests
  later, so adoption is a sink swap.
- **PostgresSink (optional)** writes a row shaped like `agent_decision_log`.

A delegated sub-agent writes its **own** record (`decision_depth = 1`,
`parent_decision_id` set), so the audit trail is a tree, not a blob.

---

## 10. Reproduction model

`reproduce(run_id)` loads `model_invocations.jsonl` and runs the engine with a
`MockContext` that puts every gateway in playback mode: the model from its
recording, tools/sources canned, targets suppressed. The output is bit-identical
to the original. Cleanest on single-shot task recordings; delegating or
suspending agents record per-segment (each run/segment is its own recording), so
their faithful replay is a roadmap item (replay the tree, not one file).

---

## 11. Sequence — the flagship agent run

```mermaid
sequenceDiagram
    participant W as Worker
    participant E as Engine
    participant SRC as Source(PG)
    participant M as Model chain
    participant SUB as loss_history_analyst
    participant MCP as MCP(policy_service)
    participant H as HITL store
    participant TGT as Target(S3)
    W->>E: run_agent(underwriting_agent, {submission_id})
    E->>SRC: resolve submission row (mock/live)
    E->>M: turn 0
    M-->>E: tool_use delegate_to_agent → loss_history_analyst
    E->>SUB: run_agent(loss_history_analyst, depth=1)
    SUB->>MCP: pull_loss_runs(named_insured)
    SUB-->>E: {loss_count, loss_ratio, ...}  (own decision record)
    E->>M: turn 1
    M-->>E: tool_use lookup_appetite
    E->>MCP: call_tool(lookup_appetite)
    E->>M: turn 2
    M-->>E: tool_use property_data
    E->>MCP: call_tool(property_data)
    E->>M: turn 3
    M-->>E: tool_use rate_property (python)
    E->>M: turn 4
    M-->>E: tool_use bind_policy  ⟵ HITL gate
    E->>H: save Continuation; raise HITLSuspended
    E-->>W: SUSPENDED
    Note over W,H: underwriter approves out of band
    W->>E: resume(suspension_id)
    E->>M: turn 5
    M-->>E: submit_output {decision, premium, ...}
    E->>TGT: write decision (suppress/live)
    E-->>W: COMPLETE  (+ decision_log.json)
```

---

## 12. What is real vs stubbed

Built and verified offline: the loop, the four gateways, the model chain with
fallback, python/MCP/builtin tool routing, S3 + Postgres connectors, source/
target binding with suppression, HITL suspend/resume, quota, decision logging,
reproduction, the Rich step-tracer, the Postgres SKIP LOCKED worker, Docker.

Stubbed (by design, ADR-sanctioned): the distributed plane — NATS JetStream,
the coordinator/lease election, the hub gateway API, mTLS enrollment, island
mode, pre-signed URLs. The worker uses the Postgres SKIP LOCKED fallback that
ADR-0015 explicitly preserves. See `ADR-COMPATIBILITY.md` for the seam-by-seam
mapping and `INTEGRATION.md` for how each stub is swapped for the real plane.
