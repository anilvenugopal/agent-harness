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

You need at least one provider key to run live demos. The offline demo needs none.

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

Open `.env` and fill in your keys:
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
GEMINI_API_KEY=AIza...
OPENAI_API_KEY=sk-proj-...      # optional

# Leave these commented unless running Docker:
# PG_MAIN_DSN=postgresql://harness:harness@localhost:5432/harness
# DECISION_SINK=file
# S3_ENDPOINT_URL=http://localhost:9000
# S3_ACCESS_KEY=minioadmin
# S3_SECRET_KEY=minioadmin
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
│   └── research_subagent.agent.yaml
│
├── scripts/
│   ├── demo_app.py             Python tool implementations (calculate_risk_score, issue_binder)
│   └── scenarios.py            Scripted offline turns for each demo scenario
│
├── mcp_servers/
│   └── example_server.py       FastMCP server — exposes lookup_policy + search tools
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
  │  │  port: 5432  │  │  port: 9000  │  │  port: 9100 → 9000   │  │
  │  │              │  │  UI:   9001  │  │                      │  │
  │  │  3 tables:   │  │              │  │  FastMCP server      │  │
  │  │  - execution_│  │  S3-compat.  │  │  Tools:              │  │
  │  │    run       │  │  object store│  │  - lookup_policy     │  │
  │  │  - agent_    │  │              │  │  - search            │  │
  │  │    decision_ │  │  Buckets:    │  │                      │  │
  │  │    log       │  │  (auto-creat)│  │  Internal URL:       │  │
  │  │  - applicant │  │              │  │  http://mcp-server   │  │
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
    artifacts  → Decision logs + suspensions (shared by worker + jupyter)
```

### Service ports at a glance

| Service | Host port | What's there |
|---|---|---|
| postgres | 5432 | Postgres DB (user: harness, pass: harness, db: harness) |
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
input_json      JSONB       The run's input dict, e.g. {"applicant_id": 1}
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

### `applicant` — demo seed data

Three sample applicants pre-loaded for the underwriting demo:

```
id │ name        │ age │ risk_band │ prior_claims
───┼─────────────┼─────┼───────────┼─────────────
 1 │ Dana Lee    │  41 │ medium    │ 1
 2 │ Sam Okoro   │  29 │ low       │ 0
 3 │ Rae Cohen   │  57 │ high      │ 3
```

The underwriting package sources query:
`SELECT id, name, age, risk_band, prior_claims FROM applicant WHERE id = {{input.applicant_id}}`

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
  "input_json": { "text": "..." },
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
postgres     Up N minutes (healthy)   0.0.0.0:5432->5432/tcp
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

### What are these demos?

**`classify_document`** — A document triage task. Imagine a company receiving
hundreds of customer messages per day: complaints, product questions, billing
inquiries, compliments. Someone (or something) needs to read each one and label
it before it gets routed to the right team. That's what this task does: you give
it a text string, and it returns a category, a confidence score, and a short
rationale. One model turn, no tools, one output.

```
Input:  text = "I have been waiting three weeks for a callback and I am furious."
Output: { category: "complaint", confidence: 0.88, rationale: "..." }
```

There are no input files. The text is passed directly as a string parameter.

---

**`underwriting_agent`** — An insurance underwriting workflow. Imagine an insurer
who needs to decide whether to offer a new customer a policy, at what premium, and
whether the case needs human sign-off before binding. The agent:
1. Fetches the applicant's record from the database (age, risk band, prior claims)
2. Delegates research to a sub-agent (looks up relevant context)
3. Calls a policy lookup tool to get the rules for the product type (auto / home)
4. Calls a risk scoring tool to compute a numeric score
5. If issuing a binder, pauses and asks a human to approve before proceeding
6. Returns a structured decision: approve / decline / refer, plus the premium

```
Input:  { applicant_id: 1, product: "auto" }
Output: { decision: "approve", premium: 1240.0, risk_score: 42.2, rationale: "..." }
```

There are no input files. The applicant record is fetched from Postgres using the
`applicant_id`. In offline mode this fetch is mocked; in live mode it hits the
actual `applicant` table seeded with three demo rows.

---

There are two modes: **offline** (mock — no API keys, no Docker needed) and
**live** (real models, full Docker stack). Both use identical engine code.

---

### MODE A: Offline Demo (no keys, no Docker)

Set the PYTHONPATH once, then run any scenario:
```bash
export PYTHONPATH=/path/to/agent-harness
cd agent-harness
```

#### Scenario 1: classify (single-turn task)

```bash
.venv/bin/python -m harness.cli demo classify
```

**What it does:**
- Runs `classify_document` (a Task — one model turn)
- Input: a complaint text (hardcoded in `scripts/scenarios.py`)
- Model: mocked (no API call)
- Output: `{category, confidence, rationale}`
- Target: S3 write suppressed (no MinIO)

**Input:**
```
"I have been waiting three weeks for a callback and I am furious. Escalate this now."
```

**Expected output:**
```
● run_started     entity=classify_document kind=task
● turn_started    turn=0
● model_attempt   provider=anthropic model=claude-opus-4-8 priority=0 attempt=0
● target_suppressed  connector=s3_main
● decision_logged    status=complete
● run_complete       status=complete tokens=20

── result ──
output: { "category": "complaint", "confidence": 0.88,
          "rationale": "Customer expresses dissatisfaction and requests escalation." }
```

---

#### Scenario 2: underwriting --auto-approve (full agent, auto-approves HITL)

```bash
.venv/bin/python -m harness.cli demo underwriting --auto-approve
```

**What it does:**
- Runs `underwriting_agent` (an Agent — multi-turn loop)
- Executes all 5 turns, suspends at HITL gate, auto-approves, resumes
- All model turns are scripted (mock provider)
- Postgres source is mocked; MCP calls use scripted tool responses; S3 suppressed

**Input:**
```json
{ "applicant_id": 1, "product": "auto" }
```

**Execution flow:**

```
Parent agent: underwriting_agent (depth 0)
│
├─ source resolved: applicant = [{id:1, name:Dana Lee, age:41, risk_band:medium, claims:1}]
│                                (mocked — no Postgres)
│
├─ Turn 0: model → delegate_to_agent(research_subagent, {topic: "auto insurance risk..."})
│   │
│   └─ Sub-agent: research_subagent (depth 1) ──────────────────────────────────────────┐
│       ├─ Turn 0: model → search({query: "auto risk applicant 1"})                     │
│       │           tool denied (no MCP client in offline demo)                         │
│       ├─ Turn 1: model → submit_output({findings: "...", sources_count: 3})           │
│       └─ decision_log written (depth=1, parent_decision_id=parent's id)               │
│                                                                               ◄────────┘
├─ Turn 1: model → lookup_policy({product: "auto"})
│           → {max_premium: 5000, min_age: 18}  (mocked tool response)
│
├─ Turn 2: model → calculate_risk_score({age:41, risk_band:medium, prior_claims:1})
│           → {risk_score: 42.2, band: medium}  (real Python function called)
│
├─ Turn 3: model → issue_binder({applicant_id:1, premium:1240.0})
│           ⏸  HITL gate fires — loop suspends
│           --auto-approve: records decision=approve, resumes immediately
│
└─ Turn 4 (after resume): model → submit_output({decision:approve, premium:1240.0, ...})
   └─ decision_log written (depth=0)
```

**Expected output:**
```
● run_started        entity=underwriting_agent kind=agent
● source_resolved    bind_to=applicant mocked=True
● turn_started       turn=0
● model_responded    stop=tool_use tools=1
● tool_called        tool=delegate_to_agent
● delegation_started parent=underwriting_agent child=research_subagent depth=1
  ● run_started      entity=research_subagent kind=agent
  ● turn_started     turn=0
  ● ...
  ● decision_logged  status=complete
● delegation_finished child=research_subagent status=complete
● turn_started       turn=1
● tool_called        tool=lookup_policy
● turn_started       turn=2
● tool_called        tool=calculate_risk_score
● turn_started       turn=3
⏸ SUSPENDED for human approval: gate='issue_binder'
   --auto-approve: recording approval and resuming...
● hitl_resumed       gate=issue_binder decision=approve
● turn_started       turn=4
● decision_logged    status=complete

── result ──
output: { "decision": "approve", "premium": 1240.0,
          "risk_score": 42.2,
          "rationale": "Medium band, single prior claim, policy rules permit binding." }
```

---

#### Scenario 3: underwriting --step (pause after each stage)

```bash
.venv/bin/python -m harness.cli demo underwriting --step
```

Same as above but pauses after each trace event and waits for Enter.
Use this with a debugger — set a breakpoint in `harness/core/trace.py:Tracer.emit`
to inspect full loop state at any stage.

---

#### Scenario 4: Manual HITL (suspend, then resume separately)

```bash
# Step 1: Run and let it suspend (no --auto-approve)
.venv/bin/python -m harness.cli demo underwriting

# Output ends with:
# ⏸  SUSPENDED: gate='issue_binder' suspension=<uuid>
# resume with: python -m harness.cli resume <uuid> --decision approve

# Step 2: List pending suspensions
.venv/bin/python -m harness.cli list-suspensions

# Output:
# <uuid>  run=<run-uuid>  agent=underwriting_agent  gate=issue_binder
#         input={'applicant_id': 1, 'premium': 1240.0}

# Step 3: Approve (or deny, or edit)
.venv/bin/python -m harness.cli resume <uuid> --decision approve
.venv/bin/python -m harness.cli resume <uuid> --decision deny --note "premium too high"
.venv/bin/python -m harness.cli resume <uuid> --decision edit \
    --edited-input '{"applicant_id": 1, "premium": 950.0}'
```

---

### MODE B: Live Demo (real models, Docker stack required)

Ensure Docker stack is up (Section 8) and `.env` has at least one API key.

#### Live single-turn: classify with Claude

```bash
set -a && source .env && set +a   # load .env into shell
PYTHONPATH=. .venv/bin/python -m harness.cli run classify_document \
    --input '{"text": "My shipment arrived damaged and I want a full refund immediately."}'
```

#### Live single-turn: classify with Gemini (force fallback)

```bash
ANTHROPIC_API_KEY="" PYTHONPATH=. .venv/bin/python -m harness.cli run classify_document \
    --input '{"text": "My shipment arrived damaged and I want a full refund immediately."}'
```

Unsetting `ANTHROPIC_API_KEY` makes the factory skip the Anthropic provider,
so the chain falls straight to Gemini.

#### Live agent: underwriting via worker queue

```bash
# Step 1: Enqueue the job (inserts a row into Postgres)
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli enqueue underwriting_agent \
  --input '{"applicant_id": 1, "product": "auto"}'

# Output: enqueued run <uuid> (agent underwriting_agent)

# Step 2: Worker picks it up automatically — watch the logs
docker compose --project-directory . -f docker/docker-compose.yml logs -f worker

# Step 3: When it suspends at HITL, list suspensions
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli list-suspensions

# Step 4: Approve
docker compose --project-directory . -f docker/docker-compose.yml \
  exec worker python -m harness.cli resume <suspension-uuid> --decision approve
```

Try different applicants (different risk profiles):

```bash
# Low risk: Sam Okoro, age 29, low band, 0 claims
--input '{"applicant_id": 2, "product": "auto"}'

# High risk: Rae Cohen, age 57, high band, 3 claims
--input '{"applicant_id": 3, "product": "auto"}'

# Home product (different policy rules):
--input '{"applicant_id": 1, "product": "home"}'
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
SELECT * FROM applicant;
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
OFFLINE DEMOS (no keys, no Docker)
  demo classify                   → task: classify a document
  demo underwriting --auto-approve → agent: full loop, auto-approve HITL
  demo underwriting --step         → same, pause after each stage
  demo underwriting                → suspend at HITL; resume manually

LIVE RUNS (keys in .env, no Docker needed)
  run classify_document --input '{"text":"..."}'   → real Claude
  ANTHROPIC_API_KEY="" run classify_document ...   → force Gemini fallback
  reproduce <run-id>                               → audit replay (no API call)

WORKER MODE (Docker stack required)
  enqueue underwriting_agent --input '{"applicant_id":1,"product":"auto"}'
  list-suspensions
  resume <suspension-id> --decision approve|deny|edit

DOCKER
  up:   docker compose --project-directory . --env-file .env -f docker/docker-compose.yml up --build -d
  ps:   docker compose --project-directory . -f docker/docker-compose.yml ps
  logs: docker compose --project-directory . -f docker/docker-compose.yml logs -f worker
  down: docker compose --project-directory . -f docker/docker-compose.yml down

ARTIFACTS (same folder for offline and Docker — bind mount)
  ./_artifacts/runs/YYYY/MM/DD/<run-id>/decision_log.json

POSTGRES (inside Docker)
  docker compose --project-directory . -f docker/docker-compose.yml exec postgres psql -U harness -d harness
  Tables: execution_run  agent_decision_log  applicant

MINIO UI
  http://localhost:9001  (minioadmin / minioadmin)

JUPYTERLAB
  http://localhost:8888
```