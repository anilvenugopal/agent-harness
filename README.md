# Multi-Provider Agent Harness

A runnable agent **execution engine** — Claude primary, OpenAI / Gemini fallback
— built to be adopted into the Verity governance platform. It owns the agentic
loop client-side, normalises every provider behind a single neutral IR, and routes
every external effect through a gateway with a mock/suppress seam. The result is a
system that is **provider-independent**, **step-debuggable**, and
**deterministically reproducible**.

---

## The one idea

**Every external effect goes through a gateway, and every gateway has a
mock/suppress seam.** A run only ever touches the outside world in four ways —
model call, tool call, source read, target write — so there are exactly four
gateways:

| Gateway | Effect | Mock seam | Suppress seam |
|---|---|---|---|
| **Model** | LLM inference | replay recorded turns | — |
| **Tool** | tool / MCP call | canned tool responses | auth-deny |
| **Source** | read an input | canned source values | — |
| **Target** | write an output | replay a write handle | shadow/challenger no-op |

Two payoffs fall out of this single idea:

1. **Provider independence.** The loop reads only a neutral IR, so Claude, OpenAI,
   Gemini, and the mock are interchangeable behind one interface. A fallback chain
   across providers is just the model gateway trying links in priority order.

2. **Audit reproduction is free.** "Reproduce decision X" is not a bespoke feature
   — it is all four gateways in playback mode at once. The same mock/suppress
   mechanism used in tests drives exact reproduction of any recorded run.

---

## Key highlights

- **Provider-agnostic loop** — `ModelChain` retries within a link (full-jitter
  backoff on 429/5xx) and falls through to the next provider on exhausted retries
  or fatal errors. Claude → OpenAI → Gemini in the flagship package.
- **Neutral IR** — `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock`,
  `ImageBlock`, `DocumentBlock`. Provider adapters translate to/from wire format;
  the loop never sees vendor objects.
- **Multimodal sources** — `as_block: document` in a source binding fetches bytes
  from S3 and attaches them natively (Anthropic document block / Gemini
  inline_data). Classify a PDF with one line of YAML.
- **HITL suspend/resume** — a gated tool serialises the full loop state (neutral
  `messages`, pending tool use, mock context) to a durable store, releases the
  worker, and resumes on any worker after a human decision. No resources held
  while the human thinks.
- **Delegation** — `delegate_to_agent` is a first-class builtin that re-enters the
  loop at depth + 1. Each sub-agent writes its own decision record with
  `parent_decision_id` set.
- **Decision logging** — the assembler accumulates a 31-field governance record
  throughout a run. `model_invocations.jsonl` records every model turn for replay.
  FileSink (default) writes the ADR-0015 artifact layout; PostgresSink is a
  one-line swap.
- **Postgres worker** — `SKIP LOCKED` claim loop; N workers process N rows
  concurrently with no coordination beyond the database row lock.
- **Deterministic reproduction** — `harness.cli reproduce <run_id>` loads
  `model_invocations.jsonl` and replays the run with all four gateways in
  playback mode.

---

## Architecture

```
                     ┌───────────────────────────────────────────────┐
Package (.yaml)  ───▶│                ExecutionEngine                 │
(the governed unit)  │                                               │
                     │  run_task() · run_agent() · resume()          │
                     │                   │                           │
                     │           ┌────────▼────────┐                 │
                     │           │  agentic loop   │  (neutral IR)   │
                     │           └────────┬────────┘                 │
                     │  ┌─────────────────┼─────────────────┐        │
                     └──┼────────┬────────┬────────┬────────┼────────┘
                        ▼        ▼        ▼        ▼        ▼
                   ┌────────┐┌────────┐┌────────┐┌────────┐┌──────────┐
                   │ MODEL  ││  TOOL  ││ SOURCE ││ TARGET ││ DECISION │
                   │gateway ││gateway ││ binder ││ binder ││ assembler│
                   └───┬────┘└───┬────┘└───┬────┘└───┬────┘└────┬─────┘
       ┌───────────────┤         │         │         │          │
       ▼      ▼      ▼ │    ┌────┴────┐ ┌──┴────┐ ┌──┴────┐ ┌──▼─────┐
   ┌──────┐┌──────┐┌───┐│   │  auth   │ │  PG   │ │  S3   │ │File /  │
   │claude││openai││...││   │ enforce │ │connect│ │connect│ │Postgres│
   └──────┘└──────┘└───┘│   └────┬────┘ └───────┘ └───────┘ │  sink  │
    (provider adapters)  │  ┌────┴─────┬───────────┐         └────────┘
                         │  ▼          ▼           ▼
                    ┌─────────┐  ┌──────────┐ ┌──────────┐
                    │ python  │  │   MCP    │ │ verity   │
                    │ tools   │  │ stdio /  │ │ builtin  │
                    │         │  │ HTTP     │ │(delegate)│
                    └─────────┘  └──────────┘ └──────────┘
```

Cross-cutting (policy applied inside the loop, not effects): **quota enforcer**
(before each model call), **HITL gate** (before a gated tool runs), **delegation**
(builtin tool that re-enters the loop), **tracer** (passive observer that emits a
structured event at every stage).

| Component | File | Responsibility |
|---|---|---|
| Neutral IR | `core/ir.py` | `Message`, content blocks, `ToolDef`, `ModelResponse`, `Usage`, `StopReason` |
| Package | `core/package.py` | unsigned YAML analog of `.vax`/`.vtx` |
| Engine | `core/engine.py` | loop, task path, delegation, HITL suspend/resume |
| Model chain | `providers/base.py` | priority fallback + retry-with-full-jitter |
| Provider adapters | `providers/{anthropic,openai,gemini,mock}_provider.py` | IR ↔ vendor wire format |
| Tool gateway | `tools/gateway.py` | auth enforcer → mock seam → transport routing |
| Connectors | `connectors/{base,postgres,s3}.py` | Category B data access |
| Binder | `connectors/binder.py` | source resolution + target writes + suppression |
| MCP client | `mcp/client.py` | stdio + Streamable HTTP, normalised results |
| HITL | `hitl/continuation.py` | durable suspend/resume checkpoint |
| Quota | `quota/enforcer.py` | per-run turn/token/cost ceilings |
| Decisions | `decisions/assembler.py` | record assembly + File/Postgres sinks |
| Mock context | `mock/context.py` | one object that flips all four gateways |
| Tracer | `core/trace.py` | Rich step-debuggable event stream |
| Worker | `worker/worker.py` | Postgres SKIP LOCKED claim loop |

---

## Quick start (no infrastructure required)

```bash
pip install pydantic pyyaml rich anthropic google-genai openai
export PYTHONPATH=$PWD
cp .env.example .env        # fill in at least one provider key + S3_ENDPOINT_URL
```

Run the demos (real API keys required; MinIO must be running for document demos):

```bash
# seed the sample document into MinIO
python scripts/seed_demo.py

# single-turn task: fetch a document from MinIO, classify it
python -m harness.cli demo classify

# COPE underwriting: bind path (Chicago restaurant, all checks pass → HITL gate fires)
python -m harness.cli demo underwriting_bind --auto-approve

# COPE underwriting: refer path (Tampa warehouse, TIV/loss/wind checks fail → refer)
python -m harness.cli demo underwriting_refer

# step through the loop one stage at a time (great with a debugger)
python -m harness.cli demo underwriting_bind --step
```

Run the test suite (no keys needed — uses MockProvider):

```bash
PYTHONPATH=$PWD python tests/test_engine.py     # or: pytest -q
```

Inspect the decision artifacts (ADR-0015 layout):

```
_artifacts/runs/2026/06/13/<run_id>/
    decision_log.json          # full governance record (31 fields)
    model_invocations.jsonl    # one line per model turn (drives replay)
```

Reproduce a past run deterministically:

```bash
python -m harness.cli reproduce <run_id>        # replays its recording
```

---

## Run it live (Docker stack)

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

This brings up Postgres (run queue + demo data), MinIO (S3), the example MCP
server (Streamable HTTP), a **worker** (Postgres `SKIP LOCKED`), and
**JupyterLab** at <http://localhost:8888>.

Enqueue and watch a worker pick it up:

```bash
docker compose exec worker python -m harness.cli enqueue underwriting_agent \
    --input '{"submission_id":1}'
```

Or run synchronously:

```bash
# seed first if using MinIO
python scripts/seed_demo.py
python -m harness.cli run classify_document --input '{"document_key":"documents/complaint.txt"}'
```

---

## What runs where

| Capability | Test suite (MockProvider) | Live (keys/infra) |
|---|---|---|
| Agentic loop, tool routing, delegation, HITL, quota | ✅ | ✅ |
| Decision log (file, ADR-0015 layout) | ✅ | ✅ |
| Decision log (Postgres) | — | ✅ `DECISION_SINK=postgres` |
| Multi-provider fallback chain | ✅ (aliased to mock) | ✅ Claude→OpenAI→Gemini |
| Postgres source / S3 target | ✅ (mocked/suppressed) | ✅ |
| MCP tools (stdio / Streamable HTTP) | ✅ (mocked) | ✅ |
| Worker (`SKIP LOCKED` dispatch) | — | ✅ |
| Reproduction (single-shot) | ✅ | ✅ |

---

## Repo layout

```
harness/
  core/        ir.py · package.py · engine.py · result.py · trace.py · factory.py
  providers/   base.py (chain) · anthropic_/openai_/gemini_/mock_provider.py
  tools/       gateway.py (auth + routing) · python_tools.py
  connectors/  base.py · postgres.py · s3.py · binder.py
  mcp/         client.py (stdio + Streamable HTTP)
  hitl/        continuation.py (durable suspend/resume)
  quota/       enforcer.py
  decisions/   assembler.py (record + File/Postgres sinks)
  mock/        context.py (one object, four gateway seams)
  worker/      worker.py (Postgres SKIP LOCKED claim loop)
  cli.py       demo · run · enqueue · worker · resume · reproduce · list-suspensions
packages/      classify_document.task.yaml · underwriting_agent.agent.yaml · loss_history_analyst.agent.yaml
mcp_servers/   example_server.py (FastMCP; stdio + http)
scripts/       demo_app.py (rate_property, bind_policy) · smoke_test.py
notebooks/     walkthrough.ipynb
docker/        Dockerfile · docker-compose.yml · initdb/01_schema.sql
specs/         ADR-COMPATIBILITY.md · INTEGRATION.md
tests/         test_engine.py
```

---

## The three example packages

- **`classify_document`** (task) — fetches a document from MinIO as a binary
  block (`as_block: document`), attaches it natively to the model (Anthropic
  document block / Gemini inline_data), and returns a structured classification.
  Demonstrates multimodal source binding and S3 target write.
- **`underwriting_agent`** (agent) — the flagship: a Postgres source (COPE
  submission row), python tools (`rate_property`, `bind_policy`), MCP tools
  (`property_data`, `lookup_appetite`), delegation to `loss_history_analyst`, a
  HITL gate on `bind_policy`, an S3 target write, and a Claude→OpenAI→Gemini
  fallback chain. Two scenarios: bind (Chicago restaurant) and refer (Tampa
  warehouse).
- **`loss_history_analyst`** (agent) — the delegation target; calls `pull_loss_runs`
  via MCP and writes its own decision record at depth 1 with `parent_decision_id`
  pointing to the parent run.

---

## Learning the codebase

The [`docs/`](docs/) directory contains eleven modules that walk through every
layer of the engine with ASCII diagrams and checkpoint questions:

| Module | Topic |
|---|---|
| [00 — Index](docs/00-index.md) | Prerequisites and reading order |
| [01 — Orientation](docs/01-orientation.md) | Repo layout, entry points |
| [02 — Neutral IR](docs/02-ir.md) | Content blocks, `ModelResponse`, `ToolDef` |
| [03 — Packages](docs/03-packages.md) | YAML schema, task vs agent, bindings |
| [04 — Execution engine](docs/04-execution-engine.md) | Loop stages, delegation, HITL gate, quota |
| [05 — Providers and chain](docs/05-providers-and-chain.md) | Adapters, retry/fallthrough, full-jitter backoff |
| [06 — Tool gateway](docs/06-tool-gateway.md) | Auth enforcement, mock seam, three transports |
| [07 — Connectors](docs/07-connectors.md) | Binder, `as_block` path, Postgres and S3 |
| [08 — HITL](docs/08-hitl.md) | Suspend/resume lifecycle, three human decisions |
| [09 — Decision log](docs/09-decision-log.md) | Assembler accumulation, FileSink layout, replay |
| [10 — Worker](docs/10-worker.md) | SKIP LOCKED claim loop, state machine, retry |
| [11 — End-to-end](docs/11-end-to-end.md) | Full underwriting run traced through every layer |

For operational tasks (seeding MinIO, running demos, checking artifacts,
reproducing a run): **[Runbook](docs/runbook.md)**.

---

## Debugging

The Rich tracer turns every loop stage into a structured event (indentation =
delegation depth). `--step` pauses after each event. For source-level debugging,
set a breakpoint in `harness/core/trace.py:Tracer.emit` or in any gateway and run
a `demo` scenario — you stop with the full neutral IR state in scope, no vendor
objects in the way. The notebook (`notebooks/walkthrough.ipynb`) is the same flow
cell-by-cell.

---

## Scope notes

- **Distributed plane is stubbed** (ADR-sanctioned): the worker uses the Postgres
  `SKIP LOCKED` fallback that ADR-0015 explicitly preserves. NATS / coordinator /
  mTLS / island mode are not built. See [`specs/ADR-COMPATIBILITY.md`](specs/ADR-COMPATIBILITY.md).
- **Sub-agent HITL is out of scope** — a synchronous delegation would block the
  parent worker for human-time; lifting it needs async delegation. See
  [`specs/INTEGRATION.md §6`](specs/INTEGRATION.md).
- **Provider adapters** target current SDK shapes with defensive parsing. They are
  exercised live via Docker with keys; the container has no vendor network access,
  so only the mock path is locally verifiable.
- **Reproduction** is bit-exact for single-shot task recordings. Delegating /
  suspending agents record per-segment; tree replay is a roadmap item.
