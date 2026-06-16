# Multi-Provider Agent Harness

A runnable agent **execution engine** — Claude primary, OpenAI / Gemini
fallback — built to be adopted into the Verity governance platform. It owns the
agentic loop client-side, treats every provider as a single neutral
infer-and-maybe-call-tools step, and routes every external effect through a
gateway with a mock/suppress seam. That makes it provider-independent,
debuggable, and **deterministically reproducible**.

Design rationale is in [`specs/DESIGN.md`](specs/DESIGN.md); ADR mapping in
[`specs/ADR-COMPATIBILITY.md`](specs/ADR-COMPATIBILITY.md); adoption path in
[`specs/INTEGRATION.md`](specs/INTEGRATION.md).

---

## Run the demos (real API keys required)

```bash
# install deps
pip install pydantic pyyaml rich anthropic google-genai openai
export PYTHONPATH=$PWD
cp .env.example .env        # fill in at least one provider key + S3_ENDPOINT_URL

# seed the sample document into MinIO (MinIO must be running)
python scripts/seed_demo.py

# a single-turn task: fetch a document from MinIO, classify it
python -m harness.cli demo classify

# COPE underwriting: bind path (Chicago restaurant, all checks pass → HITL gate fires)
python -m harness.cli demo underwriting_bind --auto-approve

# COPE underwriting: refer path (Tampa warehouse, TIV/loss/wind checks fail → refer)
python -m harness.cli demo underwriting_refer

# step through the loop one stage at a time (great with a debugger):
python -m harness.cli demo underwriting_bind --step
```

Run the test suite (no keys needed — uses MockProvider):

```bash
PYTHONPATH=$PWD python tests/test_engine.py     # or: pytest -q
```

Inspect what a run wrote — the ADR-0015 artifact layout:

```
_artifacts/runs/2026/06/13/<run_id>/
    decision_log.json          # the full governance record (31 fields)
    model_invocations.jsonl    # one line per model turn (drives replay)
```

Reproduce a past run deterministically:

```bash
python -m harness.cli reproduce <run_id>        # replays its recording
```

---

## Run it live (real models + Postgres + MinIO + MCP)

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

Or run synchronously with whatever keys are set:

```bash
# classify reads a document from MinIO — seed first: python scripts/seed_demo.py
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
specs/         DESIGN.md · ADR-COMPATIBILITY.md · INTEGRATION.md
tests/         test_engine.py
```

---

## The three example packages

- **`classify_document`** (task) — fetches a document from MinIO as a binary
  block (`as_block: document`), attaches it natively to the model (Anthropic
  document block / Gemini inline_data), and returns a structured classification.
  Demonstrates the multimodal source binding and S3 target write.
- **`underwriting_agent`** (agent) — the flagship: a Postgres source (COPE
  submission row), python tools (rate_property, bind_policy), MCP tools
  (property_data, lookup_appetite), delegation to `loss_history_analyst`, a HITL
  gate on `bind_policy`, an S3 target, and a Claude→OpenAI→Gemini fallback chain.
  Runs two scenarios: bind (submission 1, Chicago restaurant) and refer
  (submission 2, Tampa warehouse).
- **`loss_history_analyst`** (agent) — the delegation target; calls `pull_loss_runs`
  via MCP and writes its own decision record at depth 1.

---

## Debugging

The Rich tracer turns every loop stage into a structured event (indentation =
delegation depth). `--step` pauses after each. For source-level debugging, set a
breakpoint in `harness/core/trace.py:Tracer.emit` or in any gateway and run a
`demo` scenario — you stop with the full neutral state in scope, no vendor
objects in the way. The notebook (`notebooks/walkthrough.ipynb`) is the same
flow cell-by-cell.

---

## Scope notes

- **Distributed plane is stubbed** (ADR-sanctioned): the worker uses the
  Postgres `SKIP LOCKED` fallback ADR-0015 preserves; NATS / coordinator / mTLS
  / island mode are not built. See `specs/ADR-COMPATIBILITY.md`.
- **Sub-agent HITL is out of scope** — a synchronous delegation would block the
  parent; lifting it needs async delegation (see `specs/INTEGRATION.md §6`).
- **Provider adapters** target current SDK shapes with defensive parsing; they
  are exercised live via Docker with keys (the container here has no vendor
  network access, so only the mock path is locally verifiable).
- **Reproduction** is bit-exact for single-shot task recordings; delegating /
  suspending agents record per-segment.
```
