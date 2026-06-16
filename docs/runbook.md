# Agent Harness — Runbook

Complete setup, operation, and demo guide. Follow sections in order on a fresh machine.

---

## 1. Prerequisites

| Requirement | Min version | Check |
|---|---|---|
| Python | 3.11+ | `python3 --version` |
| uv (package manager) | any | `uv --version` |
| Docker | 24+ | `docker --version` |
| Docker Compose | v2 (plugin) | `docker compose version` |
| Git | any | `git --version` |

**Install uv if missing:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 2. API Keys — Where to Get Them

You need at least one provider key to run the demos.

| Provider | Where to create | Key format | Env var name |
|---|---|---|---|
| Anthropic (Claude) | console.anthropic.com → API Keys | `sk-ant-api03-...` | `ANTHROPIC_API_KEY` |
| Google (Gemini) | aistudio.google.com → Get API key | `AIza...` or `AQ...` | `GEMINI_API_KEY` |
| OpenAI | platform.openai.com → API keys | `sk-proj-...` | `OPENAI_API_KEY` |
| xAI (Grok) | console.x.ai | `xai-...` | `XAI_API_KEY` (not used by harness) |

The harness uses **Anthropic as primary, Gemini as first fallback, OpenAI as last resort**.
You can run with just Anthropic, just Gemini, or all three.

---

## 3. First-Time Setup

### Clone and enter the repo
```bash
git clone <repo-url> agent-harness
cd agent-harness
```

### Create the virtual environment
```bash
uv venv .venv
# Output: Using CPython 3.11.x — Creating virtual environment at: .venv
```

### Install dependencies
```bash
# Core + providers (enough for offline and live demos):
uv pip install "pydantic>=2.7" "pyyaml>=6.0" "rich>=13.0" "anthropic>=0.40" "google-genai>=1.0"

# Full install (adds MCP, Postgres, S3, Jupyter):
uv pip install -r requirements.txt
```

### Set up the .env file
```bash
cp .env.example .env
```

Open `.env` and fill in your keys. The CLI loads this file automatically — no
`source .env` required:
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
GEMINI_API_KEY=AIza...
OPENAI_API_KEY=sk-proj-...          # optional

# For classify demo (MinIO document source):
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin

# For underwriting demo (Postgres submission source):
# PG_MAIN_DSN=postgresql://harness:harness@localhost:5433/harness

# MCP policy service — uncomment when using Docker stack:
# MCP_POLICY_URL=http://localhost:9100/mcp
```

---

## 4. Codebase Overview

```
agent-harness/
│
├── harness/                    THE ENGINE — all runtime logic
│   ├── core/
│   │   ├── ir.py               Neutral message types (TextBlock, ToolUseBlock, etc.)
│   │   ├── engine.py           The agentic loop (run_task, run_agent, resume)
│   │   ├── package.py          Package schema — loads YAML into typed Python objects
│   │   ├── factory.py          Wires engine + providers + connectors together
│   │   ├── trace.py            Rich console tracer (the coloured loop output)
│   │   └── result.py           ExecutionResult, RunStatus, HITLSuspended
│   │
│   ├── providers/
│   │   ├── base.py             ModelChain — tries providers in priority order
│   │   ├── anthropic_provider.py   Claude adapter
│   │   ├── gemini_provider.py      Gemini adapter
│   │   ├── openai_provider.py      OpenAI adapter
│   │   └── mock_provider.py        Scripted offline provider
│   │
│   ├── tools/
│   │   ├── gateway.py          Routes tool calls → python / MCP / builtin
│   │   └── python_tools.py     Registry for python_inprocess tools
│   │
│   ├── connectors/
│   │   ├── base.py             Connector protocol + registry
│   │   ├── binder.py           Resolves sources before loop, writes targets after
│   │   ├── postgres.py         Postgres connector (fetch rows / write rows)
│   │   └── s3.py               S3/MinIO connector (put_object)
│   │
│   ├── mcp/
│   │   └── client.py           MCP client (stdio + Streamable HTTP transport)
│   │
│   ├── hitl/
│   │   └── continuation.py     Serialize/deserialize loop state for HITL suspend/resume
│   │
│   ├── quota/
│   │   └── enforcer.py         Checks max_turns, max_total_tokens, max_usd per run
│   │
│   ├── decisions/
│   │   └── assembler.py        Builds + writes the governance record
│   │
│   ├── mock/
│   │   └── context.py          MockContext — seam object controlling what gets mocked
│   │
│   ├── worker/
│   │   └── worker.py           Postgres SKIP LOCKED claim loop
│   │
│   └── cli.py                  Entry point: demo / run / enqueue / worker / resume / reproduce
│
├── packages/                   DECLARATIVE CONFIGS — what each agent/task does
│   ├── classify_document.task.yaml
│   ├── underwriting_agent.agent.yaml
│   └── loss_history_analyst.agent.yaml
│
├── samples/
│   └── complaint.txt           Sample customer complaint uploaded to MinIO by seed_demo.py
│
├── scripts/
│   ├── demo_app.py             Python tool implementations (rate_property, bind_policy)
│   └── seed_demo.py            One-time MinIO seed: uploads samples/ to the documents bucket
│
├── mcp_servers/
│   └── example_server.py       FastMCP server — exposes property_data, lookup_appetite, pull_loss_runs
│
├── docker/
│   ├── Dockerfile              One image, role by command
│   ├── docker-compose.yml      Five-service local stack
│   └── initdb/01_schema.sql    Postgres schema + demo seed data
│
├── notebooks/
│   └── walkthrough.ipynb       Cell-by-cell engine walkthrough in JupyterLab
│
├── tests/
│   └── test_engine.py          Offline pytest suite (no keys, no infra)
│
├── .env.example                Template for API keys and infra config
└── requirements.txt            All Python dependencies (tiered: core / providers / infra / dev)
```

---

## 5. Infrastructure Overview (Docker)

```
                        YOUR MACHINE (host)
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
  │  │   postgres   │  │    minio     │  │     mcp-server       │  │
  │  │  port: 5433  │  │  port: 9000  │  │  port: 9100 → 9000   │  │
  │  │              │  │  UI:   9001  │  │                      │  │
  │  │  3 tables:   │  │              │  │  FastMCP server      │  │
  │  │  - execution_│  │  S3-compat.  │  │  Tools:              │  │
  │  │    run       │  │  object store│  │  - property_data     │  │
  │  │  - agent_    │  │              │  │  - lookup_appetite   │  │
  │  │    decision_ │  │  Buckets:    │  │  - pull_loss_runs    │  │
  │  │    log       │  │  (auto-creat)│  │                      │  │
  │  │  - submission│  │              │  │  Internal URL:       │  │
  │  │              │  │              │  │  http://mcp-server   │  │
  │  │              │  │              │  │         :9000/mcp    │  │
  │  └──────┬───────┘  └──────┬───────┘  └──────────────────────┘  │
  │         │                 │                      ▲             │
  │         │   (healthcheck) │                      │ HTTP        │
  │         ▼                 ▼                      │             │
  │  ┌──────────────────────────────────────────────────────────┐  │
  │  │                       worker                             │  │
  │  │  Runs: python -m harness.cli worker                      │  │
  │  │  Polls Postgres every 1s for queued runs                 │  │
  │  │  Calls MCP server for tool requests                      │  │
  │  │  Writes artifacts to /app/_artifacts (named volume)      │  │
  │  └──────────────────────────────────────────────────────────┘  │
  │                                                                │
  │  ┌──────────────────────────────────────────────────────────┐  │
  │  │                      jupyter                             │  │
  │  │  port: 8888           http://localhost:8888              │  │
  │  │  Runs: JupyterLab with notebooks/walkthrough.ipynb       │  │
  │  └──────────────────────────────────────────────────────────┘  │
  └────────────────────────────────────────────────────────────────┘

  Named volumes (Docker-managed, persist across restarts):
    pgdata     → Postgres data files
    miniodata  → MinIO object files

  Bind mount (appears directly on host filesystem):
    ./_artifacts/ ←→ /app/_artifacts inside worker + jupyter
```

### Service ports at a glance

| Service | Host port | What's there |
|---|---|---|
| postgres | 5433 | Postgres DB (user: harness, pass: harness, db: harness) |
| minio | 9000 | S3 API endpoint |
| minio | 9001 | MinIO web console (minioadmin / minioadmin) |
| mcp-server | 9100 | MCP Streamable HTTP (`/mcp` path) |
| jupyter | 8888 | JupyterLab (no token — open directly) |

---

## 6. Postgres Tables

Three tables are created by `docker/initdb/01_schema.sql` on first startup.

### `execution_run` — the worker dispatch queue

Every job passes through this table. The worker uses `FOR UPDATE SKIP LOCKED`
to claim rows atomically without blocking other workers.

```
Column          Type        Description
─────────────────────────────────────────────────────────────────────────
id              UUID        Run identifier (generated at enqueue time)
entity_kind     TEXT        'task' or 'agent'
entity_name     TEXT        Package name, e.g. 'underwriting_agent'
channel         TEXT        'production' (default) — routing label
input_json      JSONB       The run's input dict, e.g. {"submission_id": 1}
status          TEXT        queued → executing → complete|failed|suspended|max_turns
attempt         INT         How many times this run has been tried (max 3)
worker_id       TEXT        Which worker claimed it, e.g. 'worker-1'
suspension_id   UUID        Set when status='suspended' — links to suspension file
decision_log_id UUID        Set on completion — links to the decision log file
output_json     JSONB       The final output dict (set on completion)
error_message   TEXT        Set on failure
created_at      TIMESTAMPTZ When it was enqueued
claimed_at      TIMESTAMPTZ When the worker picked it up
finished_at     TIMESTAMPTZ When it completed/failed/suspended
```

**Status lifecycle:**

```
  enqueue_run()
       │
       ▼
   [ queued ]
       │ worker.claim_one()
       ▼
  [ executing ]
       │
       ├─── success ──────────────► [ complete ]
       │
       ├─── HITL gate fires ──────► [ suspended ]
       │    (human approves →              │
       │     back to queued)        resume → [ queued ] → executing → complete
       │
       ├─── exception, attempt < 3 ► [ queued ]
       │    The run is put back in the queue with attempt+1.
       │    The next time the worker polls, it picks up the row again
       │    and tries from the beginning. This handles transient failures:
       │    network timeout, API rate limit, tool crash. Up to 3 tries.
       │
       └─── exception, attempt = 3 ► [ failed ]
            Three failures — worker gives up permanently, writes the
            error message to the row, and moves on. No more retries.
```

### `agent_decision_log` — governance records (optional sink)

Only populated when `DECISION_SINK=postgres`. Default is `file` (written to
the `artifacts` volume instead). Mirrors the same fields as the file-based
`decision_log.json`.

Key columns: `id`, `entity_name`, `input_json`, `output_json`, `models_used`,
`input_tokens`, `output_tokens`, `tool_calls_made`, `status`, `parent_decision_id`.

### `submission` — demo seed data

Two commercial property submissions pre-loaded for the underwriting demo:

```
id │ named_insured        │ state │ occupancy  │ construction     │ tiv       │ PPC │ sprinklered
───┼──────────────────────┼───────┼────────────┼──────────────────┼───────────┼─────┼────────────
 1 │ Lakeview Bistro LLC  │  IL   │ restaurant │ joisted_masonry  │ 1,850,000 │  4  │ yes
 2 │ Gulfside Storage Inc │  FL   │ warehouse  │ frame            │ 8,000,000 │  8  │ no
```

Submission 1 (Chicago restaurant, good protection class, sprinklered) is designed to pass all
underwriting checks → the demo binds the policy and fires the HITL gate.

Submission 2 (Tampa frame warehouse, high PPC, not sprinklered, large TIV, Florida wind exposure)
fails multiple checks → the demo refers without binding.

The underwriting package sources query:
`SELECT * FROM submission WHERE id = {{input.submission_id}}`

---

## 7. Artifact File Layout

All artifacts — whether from offline runs or Docker worker runs — land in
`./_artifacts/` in your project root. You can open this folder in VSCode,
Finder, or your terminal at any time.

```
_artifacts/
├── runs/
│   └── 2026/06/15/                    ← date of the run
│       └── <run-id-uuid>/
│           ├── decision_log.json       ← the full governance record (31 fields)
│           └── model_invocations.jsonl ← one line per model turn (drives replay)
│
└── suspensions/
    └── <suspension-id-uuid>.json       ← serialized loop state, pending human decision
```

**Why this works for both offline and Docker:** The `docker-compose.yml` uses a
**bind mount** — `./_artifacts` on your machine is directly linked to
`/app/_artifacts` inside the worker container. When the worker writes a file,
it appears in your project folder instantly. No copying, no `docker exec`.

```
Your project root                 Inside the worker container
─────────────────────             ────────────────────────────
./_artifacts/          ←——bind——→  /app/_artifacts/
  runs/                               runs/
  suspensions/                        suspensions/
```

Read files directly from your terminal:
```bash
# Most recent run:
ls -t _artifacts/runs/2026/06/15/ | head -1

# Pretty-print a decision log:
cat _artifacts/runs/2026/06/15/<run-id>/decision_log.json | python3 -m json.tool | less
```

### `decision_log.json` — the governance record

```json
{
  "id": "3bec33c8-...",              ← decision log UUID (≠ run_id)
  "entity_kind": "task",
  "entity_name": "classify_document",
  "entity_version": "v1",
  "channel": "production",
  "mock_mode": false,                ← true if any mock was active
  "execution_run_id": "...",         ← matches the Postgres execution_run.id
  "parent_decision_id": null,        ← set for sub-agents (delegation depth > 0)
  "decision_depth": 0,               ← 0=top-level, 1=first sub-agent, etc.
  "input_json": { "document_key": "documents/complaint.txt" },
  "output_json": { "category": "complaint", "confidence": 0.82, "rationale": "..." },
  "model_chain": [ ... ],            ← full chain configured (all providers, all priorities)
  "models_used": ["claude-opus-4-8"], ← which model(s) actually responded
  "input_tokens": 646,
  "output_tokens": 140,
  "cache_read_tokens": 0,
  "duration_ms": 2335,
  "message_history": [ ... ],        ← full conversation (every turn, every block)
  "tool_calls_made": [ ... ],        ← every tool call: name, input, output, transport
  "source_resolutions": [ ... ],     ← every source: connector, query, value, mocked?
  "target_writes": [ ... ],          ← every target write: connector, path, success?
  "status": "complete",
  "hitl_required": false,
  "created_at": "2026-06-15T14:..."
}
```

### `model_invocations.jsonl` — the replay file

One JSON line per model turn. Used by `harness.cli reproduce` to replay a run
bit-exactly without hitting the real API.

```jsonl
{"turn": 0, "provider": "anthropic", "model": "claude-opus-4-8", "stop_reason": "tool_use", "usage": {...}, "blocks": [...]}
{"turn": 1, "provider": "anthropic", "model": "claude-opus-4-8", "stop_reason": "end_turn", "usage": {...}, "blocks": [...]}
```

### `suspensions/<id>.json` — serialized loop state

Written when a HITL gate fires. Contains the full loop state needed to resume:
message history, turn number, tool calls made so far, the pending tool call
awaiting approval, and the human's recorded decision (once made).

---

## 8. Standing Up the Stack

### Start
```bash
# From the project root — the --project-directory . flag is required.
# Without it, Docker resolves 'context: .' relative to docker/ and the build fails.
docker compose --project-directory . --env-file .env -f docker/docker-compose.yml up --build -d
```

### Verify all services are healthy
```bash
docker compose --project-directory . -f docker/docker-compose.yml ps
```

Expected output:
```
SERVICE      STATUS                   PORTS
jupyter      Up N minutes             0.0.0.0:8888->8888/tcp
mcp-server   Up N minutes             0.0.0.0:9100->9000/tcp
minio        Up N minutes (healthy)   0.0.0.0:9000-9001->9000-9001/tcp
postgres     Up N minutes (healthy)   0.0.0.0:5433->5432/tcp
worker       Up N minutes
```

If `worker` shows `Exited`, check its logs:
```bash
docker compose --project-directory . -f docker/docker-compose.yml logs worker --tail=40
```

### Watch logs in real time
```bash
# Worker (shows every loop stage as it runs):
docker compose --project-directory . -f docker/docker-compose.yml logs -f worker

# MCP server (shows incoming tool calls):
docker compose --project-directory . -f docker/docker-compose.yml logs -f mcp-server
```

### Stop
```bash
# Stop containers, keep volumes (data persists):
docker compose --project-directory . -f docker/docker-compose.yml down

# Stop AND delete all data (wipe Postgres, MinIO, artifacts):
docker compose --project-directory . -f docker/docker-compose.yml down -v
```

---

## 9. Running the Demos

All demos use **real provider API keys**. At least one of `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, or `GEMINI_API_KEY` must be set.

### What are these demos?

**`classify`** — A document triage task. Fetches a real document from MinIO as a
binary block, attaches it natively to the model (Anthropic `document` block,
Gemini `inline_data`), and returns a structured classification. One model turn,
no tools.

Requires MinIO running and the document seeded:
```bash
python scripts/seed_demo.py     # uploads samples/complaint.txt → MinIO documents/complaint.txt
```

```
Input:  { "document_key": "documents/complaint.txt" }
Output: { category: "complaint", confidence: 0.95, rationale: "..." }
```

---

**`underwriting_bind`** — Commercial property underwriting, bind path. The agent
underwrites submission #1 (Lakeview Bistro LLC, Chicago IL — restaurant, joisted
masonry, PPC 4, sprinklered, $1.85M TIV). It runs the full COPE workflow:
delegates loss history analysis, checks carrier appetite, retrieves property
enrichment, rates the risk, passes all checks, and calls `bind_policy`. The HITL
gate fires — the run suspends until an underwriter approves. With `--auto-approve`
the approval is recorded automatically and the run completes.

```
Input:  { "submission_id": 1 }
Output: { decision: "bound", premium: 7438, cope_factors: {...}, ... }
```

---

**`underwriting_refer`** — Commercial property underwriting, refer path. The agent
underwrites submission #2 (Gulfside Storage Inc, Tampa FL — frame warehouse,
PPC 8, not sprinklered, $8M TIV). The COPE rating, TIV, loss ratio (0.82 — four
hurricane/water claims), and Florida wind exposure all fail the bind authority
checks. The agent calls `submit_output` directly with `decision=refer` listing
every failed check. No `bind_policy` call, no HITL gate.

```
Input:  { "submission_id": 2 }
Output: { decision: "refer", referral_reasons: [...], premium: 62726, ... }
```

---

### Demo commands

Set the PYTHONPATH (keys are loaded from `.env` automatically):

```bash
export PYTHONPATH=/path/to/agent-harness
```

#### Scenario 1: classify

Seed once (MinIO must be running):
```bash
python scripts/seed_demo.py
```

Then:
```bash
.venv/bin/python -m harness.cli demo classify
```

#### Scenario 2: underwriting bind (with auto-approve)

```bash
.venv/bin/python -m harness.cli demo underwriting_bind --auto-approve
```

**Execution flow:**

```
underwriting_agent (depth 0)
│
├─ source resolved: submission row #1 (from Postgres)
│
├─ Turn 0: delegate_to_agent → loss_history_analyst (depth 1)
│   └─ pull_loss_runs("Lakeview Bistro") → 1 claim, loss_ratio=0.15
│
├─ Turn 1: lookup_appetite(restaurant, joisted_masonry, IL)
│           → in_appetite=True, authority_tiv=3M, authority_premium=15000
│
├─ Turn 2: property_data("221 Lakeview Ave, Chicago, IL")
│           → wind_zone="none", distance_to_coast_mi=800
│
├─ Turn 3: rate_property(tiv=1850000, ...) → premium≈7438
│
├─ Turn 4: bind_policy(...)
│           ⏸ HITL gate fires — run suspends
│           --auto-approve: records approval, resumes
│
└─ Turn 5: submit_output({decision:"bound", premium:7438, ...})
   └─ 3 decision records: underwriting_agent + loss_history_analyst + (resumed)
```

#### Scenario 3: underwriting refer (no auto-approve needed)

```bash
.venv/bin/python -m harness.cli demo underwriting_refer
```

Runs through the same COPE steps for submission #2. All bind authority checks fail
(TIV $8M > authority $5M, loss_ratio 0.82 > 0.60 threshold, wind_zone="high"
and coast distance 3 mi). The agent calls `submit_output` directly — no HITL gate,
no suspension. Completes with `decision=refer` and referral_reasons populated.

#### Step mode (pause after each stage)

```bash
.venv/bin/python -m harness.cli demo underwriting_bind --step
```

Pauses after each trace event and waits for Enter. Set a breakpoint in
`harness/core/trace.py:Tracer.emit` to inspect full loop state.

#### Manual HITL (suspend, then resume separately)

```bash
# Step 1: Run without --auto-approve — it will suspend at bind_policy
.venv/bin/python -m harness.cli demo underwriting_bind

# ⏸  SUSPENDED: gate='bind_policy' suspension=<uuid>
# resume with: python -m harness.cli resume <uuid> --decision approve

# Step 2: List pending suspensions
.venv/bin/python -m harness.cli list-suspensions

# Step 3: Approve, deny, or edit
.venv/bin/python -m harness.cli resume <uuid> --decision approve
.venv/bin/python -m harness.cli resume <uuid> --decision deny --note "refer to senior uw"
.venv/bin/python -m harness.cli resume <uuid> --decision edit \
    --edited-input '{"submission_id": 1, "premium": 6900.0, "limit": 1850000, "deductible": 5000}'
```

---

### Live run via CLI (any package, any input)

```bash
# classify_document reads a document from MinIO — seed first if not done:
python scripts/seed_demo.py

PYTHONPATH=. .venv/bin/python -m harness.cli run classify_document \
    --input '{"document_key": "documents/complaint.txt"}'
```

Force Gemini fallback by unsetting the Anthropic key:

```bash
ANTHROPIC_API_KEY="" PYTHONPATH=. .venv/bin/python -m harness.cli run classify_document \
    --input '{"document_key": "documents/complaint.txt"}'
```

### Live agent via worker queue (Docker required)

```bash
# Enqueue the job
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli enqueue underwriting_agent \
  --input '{"submission_id": 1}'

# Watch the worker pick it up
docker compose --project-directory . -f docker/docker-compose.yml logs -f worker

# When it suspends, list and approve
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli list-suspensions
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli resume <suspension-uuid> --decision approve

# Refer path (no suspension):
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli enqueue underwriting_agent \
  --input '{"submission_id": 2}'
```

---

#### Audit replay: reproduce any past run

```bash
# Find a run ID from the artifacts:
ls _artifacts/runs/2026/06/15/

# Replay it (uses model_invocations.jsonl — no real API call):
PYTHONPATH=. .venv/bin/python -m harness.cli reproduce <run-id>
```

The reproduce command loads the recorded model turns and replays them through
the same engine. The output should be identical — same tool calls, same final
answer, same decision structure. A new decision log is written (marked with
`reproduced_from_decision_id`).

---

## 10. Inspecting Results

### Decision logs (offline and Docker — same folder)

Artifacts from both offline runs and Docker worker runs land in `./_artifacts/`.
There is no difference in how you read them.

```bash
# Most recent run folder:
ls -t _artifacts/runs/2026/06/15/ | head -1

# Pretty-print a decision log:
cat _artifacts/runs/2026/06/15/<run-id>/decision_log.json | python3 -m json.tool | less

# Show just the output and model used:
cat _artifacts/runs/2026/06/15/<run-id>/decision_log.json | \
  python3 -c "import json,sys; d=json.load(sys.stdin); \
  print('model:', d['models_used'], '\noutput:', json.dumps(d['output_json'], indent=2))"
```

### Postgres (worker mode)
```bash
# Connect to Postgres:
docker compose --project-directory . -f docker/docker-compose.yml \
  exec postgres psql -U harness -d harness

# Useful queries once inside psql:
SELECT id, entity_name, status, attempt, created_at FROM execution_run ORDER BY created_at DESC LIMIT 10;
SELECT id, entity_name, status, input_tokens, output_tokens FROM execution_run;
SELECT id, suspension_id, status FROM execution_run WHERE status = 'suspended';
SELECT id, named_insured, state, occupancy, tiv FROM submission;
\q
```

### MinIO (S3 output files)
Open http://localhost:9001 in a browser.
- Login: `minioadmin` / `minioadmin`
- Buckets are created automatically on first write
- Look for `underwriting/` or `classifications/` prefixes

---

## 11. Known Setup Issues

### Worker exits immediately after claiming a run

**Symptom:** `docker compose ps` shows `worker Exited (1)`

**Most likely cause:** MCP server Host header rejection (421 Misdirected Request).

**Fix** (already applied in this repo): `mcp_servers/example_server.py` must
set `allowed_hosts` to include the Docker service name:

```python
from mcp.server.transport_security import TransportSecuritySettings
mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=["mcp-server:*", "localhost:*", "127.0.0.1:*", "[::1]:*"],
    allowed_origins=["http://mcp-server:*", "http://localhost:*", "http://127.0.0.1:*"],
)
```

After editing, rebuild: `docker compose --project-directory . --env-file .env -f docker/docker-compose.yml up --build -d mcp-server worker`

### `--project-directory` is required

Always use the full command from the project root:
```bash
docker compose --project-directory . --env-file .env -f docker/docker-compose.yml <command>
```

Without `--project-directory .`, Docker resolves `context: .` relative to the
`docker/` directory and the build fails with `lstat docker/docker: no such file or directory`.

### OpenAI SDK not installed

The factory logs `[factory] openai provider unavailable: No module named 'openai'`
and continues with the remaining providers. This is not an error — install with
`uv pip install openai` if you want the OpenAI fallback.

---

## 12. Quick Reference Card

```
DEMO COMMANDS (real API keys required; .env loaded automatically)
  demo classify                         → task: fetch document from MinIO, classify it
  demo underwriting_bind --auto-approve → bind path: COPE rating, HITL, auto-approve
  demo underwriting_bind --step         → same, pause after each stage
  demo underwriting_bind                → suspend at bind_policy; resume manually
  demo underwriting_refer               → refer path: frame warehouse, no HITL gate

LIVE RUNS (keys in .env loaded automatically)
  run classify_document --input '{"document_key":"documents/complaint.txt"}'  → real Claude
  ANTHROPIC_API_KEY="" run classify_document ...                               → force Gemini
  reproduce <run-id>                                                           → audit replay

WORKER MODE (Docker stack required)
  enqueue underwriting_agent --input '{"submission_id":1}'   → bind path
  enqueue underwriting_agent --input '{"submission_id":2}'   → refer path
  list-suspensions
  resume <suspension-id> --decision approve|deny|edit

DOCKER
  up:   docker compose --project-directory . --env-file .env -f docker/docker-compose.yml up --build -d
  ps:   docker compose --project-directory . -f docker/docker-compose.yml ps
  logs: docker compose --project-directory . -f docker/docker-compose.yml logs -f worker
  down: docker compose --project-directory . -f docker/docker-compose.yml down

ARTIFACTS (bind mount — files appear directly on host)
  ./_artifacts/runs/YYYY/MM/DD/<run-id>/decision_log.json

POSTGRES (inside Docker)
  docker compose --project-directory . -f docker/docker-compose.yml exec postgres psql -U harness -d harness
  Tables: execution_run  agent_decision_log  submission

MINIO UI
  http://localhost:9001  (minioadmin / minioadmin)

JUPYTERLAB
  http://localhost:8888
```