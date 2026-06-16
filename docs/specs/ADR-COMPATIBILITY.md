# ADR Compatibility — what's real, what's stubbed, how it swaps

This engine is the **execution core** of the Verity v2 architecture, built so
that the parts deferred for this milestone are *seams*, not rewrites. Each row
below names a component, the ADR it implements or stubs, what this repo ships,
and the one change that swaps the stub for the real distributed plane.

---

## 1. Dispatch — how a run reaches a worker

| | |
|---|---|
| **ADRs** | 0015 (NATS JetStream dispatch + transactional outbox; **preserves** Postgres `SKIP LOCKED` as `VERITY_DISPATCH_MODE=postgres`), 0010 (coordinator/worker, heartbeat-lease election) |
| **Shipped** | `worker/worker.py` — atomic `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)` claim loop over an `execution_run` table |
| **Stubbed** | NATS JetStream consumer, the coordinator, lease election, island mode |
| **Swap** | Replace `Worker.claim_one()` with a JetStream pull-consumer `fetch()`. The class is structured so only the **claim source** changes; `_execute` / `_mark_*` are unchanged. |
| **Why it's safe** | ADR-0010's invariant — *in-flight execution never depends on the coordinator* — means the engine inside the worker is byte-identical under either dispatcher. The dispatcher chooses *which* run; the engine runs it the same way. |

---

## 2. Decision sink — where the governance record lands

| | |
|---|---|
| **ADRs** | 0015 (per-run artifact layout: `decision_log.json` + `model_invocations.jsonl` under `{tenant}/runs/{yyyy}/{mm}/{dd}/{run_id}/`) |
| **Shipped** | `decisions/assembler.py::FileSink` (default, exact ADR-0015 layout) and `PostgresSink` (optional, `agent_decision_log` row) |
| **Stubbed** | the hub's ingest endpoint that pulls artifacts from object storage and folds them into the central store |
| **Swap** | Add an `HubApiSink` implementing the same `DecisionSink` protocol that POSTs the assembled record to the governance API (ADR-0003), or keep `FileSink` writing to the shared object store and let the hub ingest. **The record shape is already the hub's shape**, so this is a transport change, not a schema change. |
| **Tenant prefix** | FileSink currently writes `runs/…`; production prepends `{tenant}/`. One path change in `FileSink.write`. |

---

## 3. Credentials — connectors and provider keys

| | |
|---|---|
| **ADRs** | 0010 Model B (hub stores connector **name + verification only**; the secret lives at the edge) |
| **Shipped** | connectors read their DSN/keys from the environment at construction; the engine only ever passes a connector **name** |
| **Stubbed** | the hub-side credential registry + verification handshake |
| **Swap** | `factory.build_connectors()` becomes "ask the local secrets manager for the connector named X." No engine change — the engine never sees secrets today either. |

---

## 4. Tools — the three categories

| | |
|---|---|
| **ADRs** | 0016 (Cat A = app MCP servers; Cat B = baked-in connectors SQL/REST/S3; Cat C = governance builtins) + the non-bypassable auth enforcer + claim-time MCP resolution |
| **Shipped** | `tools/gateway.py` enforcer + routing; `mcp/client.py` (stdio + Streamable HTTP); `connectors/` (Cat B SQL/S3); `delegate_to_agent` + quota + suppressor (Cat C) |
| **Stubbed** | the REST connector (only SQL + S3 shipped); image-composition list; the full Cat C suite (only delegate/quota/suppressor/assembler shipped, not e.g. the PII redactor) |
| **Swap** | add connectors implementing the `Connector` protocol; add builtins as `verity_builtin` tools routed in `ToolGateway._builtin`. The enforcer and record shape already accommodate them. |
| **MCP transport** | SSE is **not** implemented (deprecated upstream); Streamable HTTP is the remote transport, matching the v2 direction. |

---

## 5. Artifact store / object storage

| | |
|---|---|
| **ADRs** | 0015 (MinIO/S3 artifact store), 0003 (pre-signed URLs for harness↔governance object exchange) |
| **Shipped** | `connectors/s3.py` (boto3, MinIO-compatible); decision artifacts on the local FS via `FileSink` |
| **Stubbed** | pre-signed URL exchange; the harness writing artifacts the hub fetches by URL rather than shared mount |
| **Swap** | point `FileSink` at the object store (or add the `HubApiSink`); have the S3 connector mint/consume pre-signed URLs instead of direct keys. |

---

## 6. Model reference chain (multi-provider)

| | |
|---|---|
| **ADRs** | 0019 (model reference chain resolver — priority list, fall to N+1 on exhausted retries) |
| **Shipped** | `providers/base.py::ModelChain` — full priority fallback + retry-with-jitter, across Anthropic/OpenAI/Gemini/mock |
| **Stubbed** | the central model registry that supplies the chain + live prices (here the chain is in the package and prices are illustrative in the quota enforcer) |
| **Swap** | load `InferenceConfig.chain` and the price table from the registry at claim time instead of from the package literal. |

---

## 7. Packaging / signing

| | |
|---|---|
| **ADRs** | the `.vax` (agent) / `.vtx` (task) signed-artifact concept |
| **Shipped** | `core/package.py` — the **unsigned YAML analog** carrying the same logical fields (prompt, chain, tools, sources, targets, delegations, HITL, output schema) |
| **Stubbed** | signing, version pinning against a registry, signature verification at load |
| **Swap** | wrap `Package` in the signed-artifact envelope and verify on `Package.load`. The field set is already the artifact's field set. See `INTEGRATION.md §4`. |

---

## 8. HITL

| | |
|---|---|
| **ADRs** | the governance HITL flow (gate → suspend → human decision → resume) |
| **Shipped** | `hitl/continuation.py` — durable file/PG continuation, full suspend/resume, approve/deny/edit; top-level agents |
| **Stubbed** | the hub's review UI + decision API; sub-agent (depth > 0) gates (rejected by design — would block the parent) |
| **Swap** | `record_decision` is called by the review API instead of the CLI; `FileContinuationStore` → a PG/hub-backed store implementing the same protocol. Sub-agent HITL needs async delegation first. |

---

## 9. One-line summary per ADR

- **0002 / 0003** (app-hosted harness, API-only to governance): engine is hostable; all hub coupling is behind sink/store seams. ✅ seam-ready
- **0010** (federated coordinator/worker, Model B creds): worker + edge-cred model present; coordinator/lease/island stubbed. ◐ stubbed at the dispatcher
- **0015** (NATS dispatch + outbox; preserves SKIP LOCKED; artifact layout): SKIP LOCKED + exact artifact layout shipped; NATS stubbed. ◐ fallback path shipped
- **0016** (three tool categories, non-bypassable enforcer, MCP): enforcer + routing + MCP + Cat B/C subset shipped. ◐ subset
- **0019** (model reference chain): fully shipped. ✅

✅ = built · ◐ = ADR-sanctioned stub with a named swap
