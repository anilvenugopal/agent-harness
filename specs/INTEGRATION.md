# Integration — adopting the engine into Verity

This engine is built to be lifted into the Verity hub with **swaps at named
seams**, not a rewrite. This document is the adoption checklist: for each seam,
what to change and what stays untouched.

The guiding principle: **the engine core (`harness/core`, `harness/providers`,
`harness/tools`, `harness/connectors`, `harness/hitl`, `harness/quota`,
`harness/decisions`) does not change.** Integration is about replacing the
*edges* — how runs arrive, where records go, how credentials and packages are
sourced.

---

## 1. Swap the dispatch source (Postgres → NATS)

**Today:** `worker/worker.py::Worker.claim_one()` claims a run with
`SELECT … FOR UPDATE SKIP LOCKED`.

**Target (ADR-0015):** a NATS JetStream pull-consumer fed by the coordinator via
a transactional outbox.

**Change:** implement a `NatsClaimSource` with one method `claim_one() ->
Optional[dict]` that `fetch()`es one message, and inject it into `Worker`. The
loop body — `_execute`, `_mark_terminal`, `_mark_suspended`, `_mark_failed` — is
unchanged. Acknowledgement semantics: ack on terminal/suspended, `nak` (with
backoff) on crash to mirror today's requeue.

**Unchanged:** the entire `ExecutionEngine`. ADR-0010 guarantees execution never
depends on the coordinator, so the engine cannot tell which dispatcher claimed
its run.

---

## 2. Swap the decision sink (file → hub)

**Today:** `FileSink` writes `runs/{yyyy}/{mm}/{dd}/{run_id}/decision_log.json` +
`model_invocations.jsonl`.

**Target:** the hub's governance store, fed either by (a) the hub ingesting
artifacts from shared object storage, or (b) the worker POSTing the assembled
record to the governance API (ADR-0003).

**Change:** add `HubApiSink(DecisionSink)` whose `write(assembler)` serialises
`assembler.record` (already the `agent_decision_log` shape) and POSTs it; select
it in `factory` via `DECISION_SINK=hub`. Prepend the `{tenant}/` prefix in the
artifact path.

**Unchanged:** the `DecisionAssembler` and `DecisionRecord` — the record the
engine builds today is the record the hub stores.

---

## 3. Swap the credential model (env → secrets manager, ADR-0010 Model B)

**Today:** connectors read DSN/keys from the environment at construction;
`factory.build_connectors()` wires them.

**Target:** the worker resolves a connector **name** to a secret via the local
secrets manager; the hub stores only name + verification.

**Change:** `build_connectors()` becomes "for each authorized connector name,
fetch its secret locally and construct the connector." The engine already passes
only names through the binder, so nothing downstream changes.

**Unchanged:** the engine, the binder, the connector protocol.

---

## 4. Package the YAML into signed `.vax` / `.vtx`

**Today:** `Package.load(path)` reads unsigned YAML.

**Target:** signed artifacts loaded from (and pinned against) the registry.

**Change:**
1. Define the signed envelope (manifest + signature) around the existing
   `Package` field set — no field changes needed; the YAML already carries
   prompt, inference chain, tools, sources, targets, delegations, HITL policy,
   and output schema.
2. Verify the signature in a new `Package.load_signed(...)`; reject on mismatch.
3. Pin `delegations[].pinned_version` and chain model versions against the
   registry at claim time.

**Unchanged:** every consumer of `Package` (engine, factory, CLI).

---

## 5. Source the model chain + prices from the registry (ADR-0019)

**Today:** `InferenceConfig.chain` is a package literal; prices in
`quota/enforcer.py` are illustrative.

**Change:** at claim time, resolve the chain and the price table from the model
registry and overlay them onto the package's `InferenceConfig` before building
the `QuotaEnforcer`. The chain resolver and enforcer logic are unchanged.

---

## 6. Continuation store + HITL review API

**Today:** `FileContinuationStore`; decisions recorded via the CLI.

**Change:** implement the `ContinuationStore` protocol against the hub
(`save` / `load` / `record_decision` / `list_pending`); the review UI calls
`record_decision`, then flips the run runnable (`worker.requeue_resumed`
analog). The worker re-claims and calls `engine.resume`.

**Unchanged:** the suspend/resume machinery in the engine.

**Prerequisite for sub-agent HITL:** async delegation. Today a delegated
sub-agent runs synchronously inside the parent's worker, so a sub-agent gate
would block the parent for human-time — the engine rejects `depth > 0` gates.
Lifting this means delegation enqueues a child run and the parent itself
suspends awaiting the child, reusing the very same continuation mechanism.

---

## 7. Hardening checklist (before production)

- **Parameterise SQL.** The demo binder string-templates `{{input.x}}` into
  queries for ergonomics; production must bind parameters (the `PostgresConnector`
  already accepts a params path for writes — extend to reads).
- **Connection pooling.** Connectors connect-per-call for clarity; swap in a
  pool (`psycopg_pool`) and a boto3 session/client cache.
- **Provider SDK pinning.** The three adapters target current SDK shapes with
  defensive parsing and version-caveat comments; pin SDK versions and add a
  contract test per provider (a recorded request/response fixture) so a vendor
  shape change is caught in CI, contained to one adapter file.
- **Secrets.** No secret ever enters a decision record or a URL query string;
  keep it that way (the binder previews values truncated — audit that previews
  never leak source secrets in your data).
- **Idempotency.** Worker requeue can re-run a partially-effected run; make
  target writes idempotent (keyed by `run_id`) before enabling retries on
  side-effecting agents.

---

## 8. What you get for free on day one

Because the engine is already the production engine (only the edges are
stubbed), adopting it gives you, unchanged: the neutral-IR loop, the
multi-provider fallback chain, the non-bypassable tool auth enforcer, MCP over
Streamable HTTP, governed delegation with depth/authorization guards, durable
HITL suspend/resume, per-run quota, the full 31-field decision record, and
deterministic single-shot reproduction — all of which run offline today and are
covered by the test suite in `tests/`.
