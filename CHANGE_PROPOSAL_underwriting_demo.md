# Change Proposal — Make the underwriting demo credible (commercial property / COPE)

**Target repo:** the `agent-harness` project (multi-provider agent harness).
**Goal:** replace the toy `age / risk_band / prior_claims` underwriting demo with a
domain-credible **small commercial property** underwriting flow, **without changing
the engine**. All changes are to example packages, demo tools, scenarios, the example
MCP server, and the demo DB schema.

**Hard constraint:** do **not** modify anything under `harness/core`, `harness/providers`
(except model strings are NOT here), `harness/tools`, `harness/connectors`,
`harness/hitl`, `harness/quota`, `harness/decisions`, `harness/mock`, or
`harness/worker`. The engine is correct and tested; this is a content change only.

---

## 1. The underwriting story

A small-commercial **property** underwriter receives a **submission**: a specific
building to be insured, with the coverage requested. The agent works a real
intake-to-decision flow:

1. **Read the submission** (the risk + requested coverage) from the system of record.
2. **Enrich with third-party data** — pull the verified **protection class (PPC)**,
   wind/flood exposure, and prior catastrophe history for the location.
3. **Analyze loss history** — delegate to a loss-history analyst that pulls the
   5-year **loss runs** and returns a loss count, incurred total, **loss ratio**, and
   a narrative of any large losses.
4. **Check appetite & authority** — look up the carrier's guidelines for this
   line/occupancy/construction/state: is it **in appetite**, what **referral triggers**
   apply, and what is the underwriter's **binding authority** (max TIV / premium).
5. **Rate the risk** — compute premium from exposure using the **COPE** framework
   (Construction, Occupancy, Protection, Exposure): `premium = (TIV/100) × base_rate ×
   construction_factor × occupancy_factor × protection_factor × (1 − deductible_credit)`,
   returning the factor breakdown so the price is explainable.
6. **Decide**:
   - If the risk is **in appetite and within authority** with acceptable loss history →
     the agent **binds** the policy. Binding is the side-effecting, governed action, so
     it passes through the **HITL gate** (underwriter sign-off on the bind) → suspend →
     human approves → resume → bound.
   - If a **referral trigger** fires (TIV/premium over authority, high loss ratio,
     coastal/cat exposure, out-of-appetite occupancy) → the agent does **not** bind; it
     emits `decision = "refer"` (or `"decline"`) with explicit `referral_reasons` and
     completes **without** suspending.

This delivers the "clean risks get bound (with sign-off); problem risks are referred"
behavior using only the existing name-based HITL gate: **you only call the gated
`bind_policy` tool when you actually intend to bind.** No engine change is needed.

### Domain terms used (all standard P&C commercial property)
- **COPE** — Construction, Occupancy, Protection, Exposure (the canonical rating frame).
- **TIV** — Total Insured Value (the limit basis here).
- **PPC** — Public Protection Classification (ISO fire-protection class 1–10).
- **Construction classes** — frame, joisted masonry, masonry non-combustible,
  non-combustible, modified fire-resistive, fire-resistive.
- **Loss runs / loss ratio** — carrier-provided claim history; incurred ÷ earned premium.
- **Appetite / binding authority / referral** — what the carrier will write, and the
  premium/TIV thresholds above which a line underwriter must escalate.

### Deliberately NOT added (keep it a demo)
No real ISO rate tables, territory schedules, credit-based insurance scoring, multi-
building schedules, or reinsurance. Rating factors stay as **named illustrative
constants** in the python tool — same simplicity as the current toy score, only
transparent and domain-shaped.

---

## 2. File-by-file changes

### 2.1 `packages/underwriting_agent.agent.yaml` (rewrite)

- **`description`/`system_prompt`**: rewrite for commercial-property underwriting per
  the story above (intake → enrich → loss analysis → appetite/authority → rate → bind
  or refer; call `bind_policy` only when binding; otherwise `submit_output`).
- **`prompt_template`**: reference the submission, e.g.
  ```
  Underwrite this commercial property submission:
  submission_id: {{input.submission_id}}

  Submission record:
  {{context.submission}}
  ```
- **`inference.chain`** — fix stale model strings:
  - `anthropic` → `claude-opus-4-8` (keep), priority 0
  - `openai` → `gpt-4.1` (replaces placeholder `gpt-5.1`), priority 1
  - `gemini` → `gemini-2.5-pro` (replaces placeholder `gemini-3-pro`), priority 2
  - keep `max_turns: 10`, `max_usd: 2.00`.
- **`sources`** — pull the submission row:
  ```yaml
  sources:
    - connector: pg_main
      method: query
      ref: "SELECT * FROM submission WHERE id = {{input.submission_id}}"
      bind_to: submission
      required: true
  ```
- **`tools`** — replace the old set with:
  | name | transport | mcp_server | input fields |
  |---|---|---|---|
  | `property_data` | `mcp_http` | `policy_service` | `address` |
  | `lookup_appetite` | `mcp_http` | `policy_service` | `line`, `occupancy`, `construction`, `state` |
  | `rate_property` | `python_inprocess` | — | `tiv`, `occupancy`, `construction`, `protection_class`, `sprinklered`, `deductible` |
  | `delegate_to_agent` | `verity_builtin` | — | `agent_name`, `context` |
  | `bind_policy` | `python_inprocess` | — | `submission_id`, `premium`, `limit`, `deductible` |
  Keep `mcp_server: policy_service` and set `mcp_tool_name` equal to each tool's name.
- **`delegations`**: `- child_agent: loss_history_analyst`
- **`hitl`**: `enabled: true`, `require_approval_for: [bind_policy]`
- **`targets`**: keep the S3 write, rename the key prefix to `underwriting/{{run_id}}.json`
  (unchanged is fine).
- **`output_schema`**:
  ```yaml
  output_schema:
    decision:        {type: string, enum: [bound, quote, refer, decline]}
    premium:         {type: number}
    coverage_summary:{type: object}
    cope_factors:    {type: object}
    referral_reasons:{type: array, items: {type: string}}
    rationale:       {type: string}
  ```

### 2.2 Rename `packages/research_subagent.agent.yaml` → `packages/loss_history_analyst.agent.yaml`

- Set `name: loss_history_analyst`.
- `system_prompt`: "You analyze commercial property loss history. Pull the loss runs,
  then call submit_output with a concise loss summary."
- Replace the `search` tool with:
  ```yaml
  tools:
    - name: pull_loss_runs
      transport: mcp_http
      mcp_server: policy_service
      mcp_tool_name: pull_loss_runs
      input_schema:
        type: object
        properties: {named_insured: {type: string}}
        required: [named_insured]
  ```
- `output_schema`:
  ```yaml
  output_schema:
    loss_count:    {type: integer}
    incurred_total:{type: number}
    loss_ratio:    {type: number}
    large_losses:  {type: array, items: {type: object}}
    narrative:     {type: string}
  ```
- Keep the chain (`claude-haiku-4-5-20251001` priority 0, `gemini-2.5-flash` priority 1).

> **Update every reference to the old name `research_subagent`** to
> `loss_history_analyst`: the parent package's `delegations`, and the scripted
> `delegate_to_agent` calls in `scripts/scenarios.py`. Package identity is the `name:`
> field (loader keys packages by it), so the `name:` change is what matters; the file
> rename is for tidiness.

### 2.3 `scripts/demo_app.py` (replace the two tools)

Remove `calculate_risk_score` and `issue_binder`. Add:

```python
# Illustrative COPE factor tables (NOT real rate tables — demo constants).
_BASE_RATE = 0.35  # premium per $100 of TIV, before factors
_CONSTRUCTION = {"frame": 1.30, "joisted_masonry": 1.15, "masonry_nc": 1.00,
                 "non_combustible": 0.95, "modified_fire_resistive": 0.90,
                 "fire_resistive": 0.85}
_OCCUPANCY = {"office": 0.90, "retail": 1.00, "warehouse": 1.05, "light_manufacturing": 1.15,
              "habitational": 1.25, "restaurant": 1.30}
def _protection_factor(ppc: int) -> float:
    return 0.95 if ppc <= 4 else 1.10 if ppc <= 7 else 1.35 if ppc <= 9 else 1.75
_DED_CREDIT = {1000: 0.0, 2500: 0.05, 5000: 0.10, 10000: 0.15}

def rate_property(tiv, occupancy, construction, protection_class, sprinklered, deductible) -> dict:
    cf = _CONSTRUCTION.get(str(construction), 1.15)
    of = _OCCUPANCY.get(str(occupancy), 1.00)
    pf = _protection_factor(int(protection_class))
    if sprinklered:
        pf *= 0.90  # sprinkler credit folded into the protection factor
    dc = _DED_CREDIT.get(int(deductible), 0.0)
    premium = (tiv / 100) * _BASE_RATE * cf * of * pf * (1 - dc)
    return {"premium": round(premium, 2),
            "cope_factors": {"base_rate": _BASE_RATE, "construction": cf, "occupancy": of,
                             "protection": round(pf, 3), "deductible_credit": dc, "tiv": tiv}}

def bind_policy(submission_id, premium, limit, deductible) -> dict:
    return {"binder_id": f"BND-{submission_id}", "bound": True,
            "premium": premium, "limit": limit, "deductible": deductible}
```

Update `build_demo_tools()` to register `rate_property` and `bind_policy`.

### 2.4 `scripts/scenarios.py` (replace `underwriting_scenario`, add a referral scenario)

> **CRITICAL — mock-provider call order.** The offline demo uses ONE shared
> `MockProvider` that serves scripted turns **in call order across the parent AND any
> delegated sub-agent**, and the SAME provider instance continues across a HITL resume
> (its internal counter does not reset). So each scenario's `turns` list must be the
> turns **interleaved in the exact order the loop will request them**, with the
> post-resume turn(s) at the end. Get this order wrong and the demo desyncs.

Define two scenarios and register both in `SCENARIOS`.

**`underwriting_bind`** — clean risk → bind → HITL → approve. Call order:

```
parent t0: delegate_to_agent(loss_history_analyst, {named_insured: "Lakeview Bistro LLC"})
  child t0: pull_loss_runs({named_insured: "Lakeview Bistro LLC"})
  child t1: submit_output({loss_count: 2, incurred_total: 18000, loss_ratio: 0.34,
                           large_losses: [], narrative: "Two minor losses; no fire/cat."})
parent t1: lookup_appetite({line: "commercial_property", occupancy: "restaurant",
                            construction: "joisted_masonry", state: "IL"})
parent t2: property_data({address: "221 Lakeview Ave, Chicago, IL"})
parent t3: rate_property({tiv: 1850000, occupancy: "restaurant", construction: "joisted_masonry",
                          protection_class: 4, sprinklered: true, deductible: 5000})
parent t4: bind_policy({submission_id: 1, premium: 7449, limit: 1850000, deductible: 5000})
           ── HITL gate fires here (bind_policy) → SUSPEND ──
parent t5: submit_output({decision: "bound", premium: 7449,
                          coverage_summary: {limit: 1850000, deductible: 5000, valuation: "replacement_cost"},
                          cope_factors: {construction: 1.15, occupancy: 1.30, protection: 0.855, deductible_credit: 0.10},
                          referral_reasons: [], rationale: "In appetite, within authority, low loss ratio; bound with UW sign-off."})
```

`turns` list order: `[t0, child_t0, child_t1, t1, t2, t3, t4, t5]` (t5 is consumed on resume).
`input` = `{"submission_id": 1}`.
`MockContext`:
```python
MockContext(
  source_responses={"submission": [{
      "id": 1, "named_insured": "Lakeview Bistro LLC", "line_of_business": "commercial_property",
      "state": "IL", "address": "221 Lakeview Ave, Chicago, IL",
      "occupancy": "restaurant", "construction": "joisted_masonry", "year_built": 1998,
      "square_feet": 4200, "protection_class": 4, "sprinklered": True,
      "tiv": 1850000, "coverage_limit": 1850000, "deductible": 5000,
      "valuation": "replacement_cost", "coinsurance": 0.9}]},
  tool_responses={
      "pull_loss_runs": {"loss_count": 2, "incurred_total": 18000, "loss_ratio": 0.34,
                         "large_losses": [], "narrative": "Two minor losses; no fire/cat."},
      "lookup_appetite": {"in_appetite": True, "authority_tiv": 5000000, "authority_premium": 50000,
                          "referral_triggers": []},
      "property_data": {"protection_class": 4, "wind_zone": "none",
                        "distance_to_coast_mi": 120, "prior_cat": False}},
  suppress_targets=True,
)
```
(`rate_property` and `bind_policy` are python tools and run for real — do **not** mock
them. `bind_policy` runs on resume after approval.)

**`underwriting_refer`** — problem risk → refer, no bind, no suspension. Call order:

```
parent t0: delegate_to_agent(loss_history_analyst, {named_insured: "Gulfside Storage Inc"})
  child t0: pull_loss_runs({named_insured: "Gulfside Storage Inc"})
  child t1: submit_output({loss_count: 5, incurred_total: 420000, loss_ratio: 0.78,
                           large_losses: [{"year": 2023, "amount": 310000, "cause": "wind"}],
                           narrative: "Elevated loss ratio; one large wind loss."})
parent t1: lookup_appetite({line: "commercial_property", occupancy: "warehouse",
                            construction: "frame", state: "FL"})
parent t2: property_data({address: "9 Harbor Rd, Tampa, FL"})
parent t3: rate_property({tiv: 8000000, occupancy: "warehouse", construction: "frame",
                          protection_class: 8, sprinklered: false, deductible: 10000})
parent t4: submit_output({decision: "refer", premium: 43857,
                          coverage_summary: {limit: 8000000, deductible: 10000, valuation: "replacement_cost"},
                          cope_factors: {construction: 1.30, occupancy: 1.05, protection: 1.35, deductible_credit: 0.15},
                          referral_reasons: ["TIV $8.0M exceeds $5.0M binding authority",
                                             "Loss ratio 0.78 above 0.60 threshold",
                                             "Coastal exposure: 2 mi to coast, wind tier 1"],
                          rationale: "Multiple referral triggers; escalate to senior underwriter, do not bind."})
```

`turns` order: `[t0, child_t0, child_t1, t1, t2, t3, t4]` (no resume turn — no gate).
`input` = `{"submission_id": 2}`.
`MockContext`:
```python
MockContext(
  source_responses={"submission": [{
      "id": 2, "named_insured": "Gulfside Storage Inc", "line_of_business": "commercial_property",
      "state": "FL", "address": "9 Harbor Rd, Tampa, FL",
      "occupancy": "warehouse", "construction": "frame", "year_built": 1975,
      "square_feet": 60000, "protection_class": 8, "sprinklered": False,
      "tiv": 8000000, "coverage_limit": 8000000, "deductible": 10000,
      "valuation": "replacement_cost", "coinsurance": 0.9}]},
  tool_responses={
      "pull_loss_runs": {"loss_count": 5, "incurred_total": 420000, "loss_ratio": 0.78,
                         "large_losses": [{"year": 2023, "amount": 310000, "cause": "wind"}],
                         "narrative": "Elevated loss ratio; one large wind loss."},
      "lookup_appetite": {"in_appetite": True, "authority_tiv": 5000000, "authority_premium": 50000,
                          "referral_triggers": ["TIV_over_authority"]},
      "property_data": {"protection_class": 8, "wind_zone": "tier1",
                        "distance_to_coast_mi": 2, "prior_cat": True}},
  suppress_targets=True,
)
```

Update `SCENARIOS` to:
```python
SCENARIOS = {
    "classify": classify_scenario,
    "underwriting_bind": underwriting_bind_scenario,
    "underwriting_refer": underwriting_refer_scenario,
}
```
(Keep `classify_scenario` unchanged.)

### 2.5 `mcp_servers/example_server.py` (replace the server tools)

Keep the server name `policy_service`. Remove `lookup_policy` and `search`; add three
tools returning plausible canned data so the **live** Docker path also works:

```python
@mcp.tool()
def property_data(address: str) -> dict:
    """Third-party property data: verified protection class + cat exposure."""
    coastal = any(s in address for s in ("FL", "Tampa", "Harbor", "Coast"))
    return {"address": address,
            "protection_class": 8 if coastal else 4,
            "wind_zone": "tier1" if coastal else "none",
            "distance_to_coast_mi": 2 if coastal else 120,
            "prior_cat": coastal}

@mcp.tool()
def lookup_appetite(line: str, occupancy: str, construction: str, state: str) -> dict:
    """Carrier underwriting guidelines: appetite, referral triggers, binding authority."""
    triggers = []
    if occupancy in ("nightclub", "fireworks"):  # illustrative out-of-appetite classes
        return {"in_appetite": False, "authority_tiv": 0, "authority_premium": 0,
                "referral_triggers": ["occupancy_out_of_appetite"]}
    return {"in_appetite": True, "authority_tiv": 5_000_000, "authority_premium": 50_000,
            "referral_triggers": triggers}

@mcp.tool()
def pull_loss_runs(named_insured: str) -> dict:
    """5-year loss runs for the named insured (canned)."""
    heavy = "Gulfside" in named_insured or "Storage" in named_insured
    if heavy:
        return {"loss_count": 5, "incurred_total": 420000, "loss_ratio": 0.78,
                "large_losses": [{"year": 2023, "amount": 310000, "cause": "wind"}],
                "narrative": "Elevated loss ratio; one large wind loss."}
    return {"loss_count": 2, "incurred_total": 18000, "loss_ratio": 0.34,
            "large_losses": [], "narrative": "Two minor losses; no fire/cat."}
```

### 2.6 `docker/initdb/01_schema.sql` (replace `applicant` with `submission`)

Drop the `applicant` table + its rows; keep `execution_run` and `agent_decision_log`
unchanged. Add:

```sql
CREATE TABLE IF NOT EXISTS submission (
    id              INT PRIMARY KEY,
    named_insured   TEXT NOT NULL,
    line_of_business TEXT NOT NULL,
    state           TEXT NOT NULL,
    address         TEXT NOT NULL,
    occupancy       TEXT NOT NULL,
    construction    TEXT NOT NULL,
    year_built      INT,
    square_feet     INT,
    protection_class INT,
    sprinklered     BOOLEAN NOT NULL DEFAULT false,
    tiv             NUMERIC NOT NULL,
    coverage_limit  NUMERIC NOT NULL,
    deductible      INT NOT NULL,
    valuation       TEXT NOT NULL DEFAULT 'replacement_cost',
    coinsurance     NUMERIC NOT NULL DEFAULT 0.9
);
INSERT INTO submission VALUES
 (1,'Lakeview Bistro LLC','commercial_property','IL','221 Lakeview Ave, Chicago, IL',
  'restaurant','joisted_masonry',1998,4200,4,true,1850000,1850000,5000,'replacement_cost',0.9),
 (2,'Gulfside Storage Inc','commercial_property','FL','9 Harbor Rd, Tampa, FL',
  'warehouse','frame',1975,60000,8,false,8000000,8000000,10000,'replacement_cost',0.9)
ON CONFLICT (id) DO NOTHING;
```

### 2.7 Touch-ups

- `harness/cli.py`: the `demo` help text mentions `classify | underwriting`; update to
  `classify | underwriting_bind | underwriting_refer`. (The dispatch already handles any
  key in `SCENARIOS`; no logic change.)
- `README.md`: update the `demo underwriting --auto-approve` examples to the two new
  scenario names and the commercial-property framing.
- `specs/DESIGN.md` §11 sequence diagram and the example description: rename
  `research`→`loss_history_analyst`, `lookup_policy`→`lookup_appetite`,
  `calculate_risk_score`→`rate_property`, `issue_binder`→`bind_policy` (cosmetic).
- `tests/test_engine.py`: **no change required** — those tests build their own inline
  packages and do not import the example packages/scenarios. (Optional: add one test that
  loads `packages/` and runs `underwriting_refer` asserting `status == COMPLETE`,
  `decision == "refer"`, and no suspension; and `underwriting_bind` asserting it raises
  `HITLSuspended` on `bind_policy`.)

---

## 3. Expected results from demo runs

Run from the repo root with `export PYTHONPATH=$PWD`.

### `python -m harness.cli demo underwriting_bind --auto-approve`
Trace (depth-1 sub-agent indented) shows, in order:
`run_started` → `source_resolved bind_to=submission` → `delegation_started child=loss_history_analyst`
→ (sub-agent) `... run_complete` → `delegation_finished` → `tool_called lookup_appetite`
→ `tool_called property_data` → `tool_called rate_property` → `tool_called bind_policy`
→ `hitl_gate tool=bind_policy` → `hitl_suspended` → `hitl_resumed decision=approve`
→ `target_suppressed connector=s3_main` → `run_complete status=complete`.

Final result:
- `status: complete`
- `output.decision: "bound"`
- `output.premium: ≈ 7449` (rate_property computes `(1_850_000/100)·0.35·1.15·1.30·(0.95·0.90)·(1−0.10) ≈ 7448.86`)
- `output.referral_reasons: []`
- `output.cope_factors` present with construction 1.15, occupancy 1.30, protection ≈0.855, deductible_credit 0.10.

Artifacts: **3** `decision_log.json` files under `_artifacts/runs/.../`:
the loss-history sub-agent (`decision_depth: 1`, `parent_decision_id` set), the suspended
parent segment, and the resumed tail. `bind_policy` appears in `tool_calls_made`;
`target_writes[0].suppressed: true`.

### `python -m harness.cli demo underwriting_refer`
(No `--auto-approve` needed — there is no gate.) Trace shows the delegation, the three
enrichment/appetite/rating tool calls, then `submit_output` and `run_complete` —
**no `hitl_gate`, no `hitl_suspended`**.

Final result:
- `status: complete`
- `output.decision: "refer"`
- `output.premium: ≈ 43857` (indicative; not bound)
- `output.referral_reasons:` three entries (TIV over authority, loss ratio > 0.60,
  coastal exposure).

Artifacts: **2** `decision_log.json` files (sub-agent at depth 1 + the single parent run).

### `python -m harness.cli demo underwriting_bind --step`
Same as the bind run but pauses after each stage (Enter to advance) — for single-step
debugging / breakpoints in `harness/core/trace.py:Tracer.emit` or any gateway.

### Live parity (optional, needs Docker + keys)
`docker compose --env-file .env -f docker/docker-compose.yml up --build`, then
`docker compose exec worker python -m harness.cli enqueue underwriting_agent --input '{"submission_id":1}'`.
The worker pulls the submission from Postgres, calls the real `policy_service` MCP tools,
rates, and suspends at `bind_policy` (resolve via `list-suspensions` + `resume`).

---

## 4. Acceptance checklist

- [ ] No files under `harness/` changed except `cli.py` help text.
- [ ] `python tests/test_engine.py` → **9 tests pass** (unchanged).
- [ ] `demo underwriting_bind --auto-approve` → `decision: "bound"`, premium ≈ 7449,
      hits the HITL gate, resumes, completes; 3 decision records.
- [ ] `demo underwriting_refer` → `decision: "refer"`, 3 referral reasons, **no**
      suspension; 2 decision records.
- [ ] `demo classify` still works (untouched).
- [ ] No remaining references to `applicant`, `risk_band`, `prior_claims`,
      `calculate_risk_score`, `issue_binder`, `lookup_policy`, `research_subagent`, or
      `product`.
- [ ] Example package model strings updated (`gpt-5.5`, current Gemini); verify the
      Gemini model id against the target account.

---

## 5. Why this needs no engine change (rationale for the reviewer)

The engine already gates HITL by **tool name** (`hitl.require_approval_for`), routes
tools by transport, mocks any gateway via `MockContext`, and runs delegated sub-agents
as governed nested runs. The credible flow uses exactly those primitives: the
conditional "bind vs refer" behavior is expressed by **which tool the agent calls**
(`bind_policy` is reached only when binding), not by new conditional-gating logic.
Everything new lives in example content (packages, demo python tools, scenarios, the MCP
server, the seed DB), so the engine's test surface is untouched.

---

## 6. Amendments (agreed during review)

The following decisions supersede or narrow the original proposal.

### 6.1 MockProvider removed from the demo path

`MockProvider` and `scripts/scenarios.py` are removed from the demo path entirely.
Every demo run requires at least one real API key and incurs a small cost.

**Rationale:** The scripted mock-turn approach requires each scenario's interleaved
turn list to be manually kept in sync with the package, tools, and HITL call order.
Any mismatch silently desyncs the demo. Requiring real keys eliminates this fragile
coupling and means the demo shows what the system actually does.

**Scope of removal:**
- `scripts/scenarios.py` — deleted entirely.
- `harness/cli.py` `demo` command — reworked: drops MockProvider/MockContext wiring,
  calls `run_agent` / `run_task` directly with hardcoded preset inputs. The
  `--auto-approve` and `--step` flags are preserved (they are handled by the
  continuation store and tracer, not by MockProvider).
- `MockProvider` itself is **not deleted** — it remains in `harness/providers/` for
  use by `tests/test_engine.py`, which must stay fast and free.

**`reproduce` is unaffected** — it reads from `model_invocations.jsonl` via a
separate replay mechanism independent of MockProvider.

### 6.2 Model IDs (resolved)

All model strings are now pinned to confirmed current IDs:

| Provider | Model ID | Used in |
|---|---|---|
| Anthropic | `claude-opus-4-8` | underwriting_agent (primary) |
| Anthropic | `claude-haiku-4-5-20251001` | loss_history_analyst (primary) |
| OpenAI | `gpt-4.1` | underwriting_agent (fallback 1) |
| Gemini | `gemini-2.5-pro` | underwriting_agent (fallback 2) |
| Gemini | `gemini-2.5-flash` | loss_history_analyst (fallback 1), classify_document (fallback 1) |

---

## 7. Implementation task list

Tasks are ordered by dependency. Each task maps to one logical change unit.

### Prerequisites (user action)

| # | Task | Blocks |
|---|---|---|
| T0 | Confirm current OpenAI model ID (platform.openai.com/docs/models) | T2 |
| T0 | Confirm current Gemini model ID (ai.google.dev/gemini-api/docs/models) | T2, T3 |

### Phase 1 — Data

| # | Task | File |
|---|---|---|
| T1 | Replace `applicant` table with `submission` table; add 2 seed rows | `docker/initdb/01_schema.sql` |

### Phase 2 — Packages

| # | Task | File |
|---|---|---|
| T2 | Rewrite underwriting_agent package (COPE flow, submission source, 5 tools, `loss_history_analyst` delegation, `bind_policy` HITL, new output_schema) | `packages/underwriting_agent.agent.yaml` |
| T3 | Replace research_subagent with loss_history_analyst (`pull_loss_runs` tool, loss-history output schema) | `packages/loss_history_analyst.agent.yaml` (delete `research_subagent.agent.yaml`) |

### Phase 3 — Python tools

| # | Task | File |
|---|---|---|
| T4 | Remove `calculate_risk_score` + `issue_binder`; add `rate_property` (COPE factor tables) + `bind_policy` | `scripts/demo_app.py` |

### Phase 4 — MCP server

| # | Task | File |
|---|---|---|
| T5 | Remove `lookup_policy` + `search`; add `property_data` + `lookup_appetite` + `pull_loss_runs` | `mcp_servers/example_server.py` |

### Phase 5 — Demo path rework (MockProvider removal)

| # | Task | File |
|---|---|---|
| T6 | Delete offline scenario scripts | `scripts/scenarios.py` (delete) |
| T7 | Rework `demo` command: remove MockProvider/scenarios wiring; call `run_agent`/`run_task` with preset inputs; preserve `--auto-approve` and `--step` | `harness/cli.py` |

### Phase 6 — Documentation

| # | Task | File |
|---|---|---|
| T8 | Update demo commands, submission table schema, remove offline section | `docs/runbook.md` |
| T9 | Update packages comparison table for new packages | `docs/03-packages.md` |

### Phase 7 — Acceptance

| # | Check | Pass criterion |
|---|---|---|
| T10 | `demo underwriting_bind --auto-approve` | `decision: "bound"`, premium ≈ 7449, HITL fires and resumes, 3 decision records |
| T11 | `demo underwriting_refer` | `decision: "refer"`, 3 referral reasons, no suspension, 2 decision records |
| T12 | `demo classify` | Still works unchanged |
| T13 | `pytest tests/` + symbol scan | All tests pass; no references to `applicant`, `risk_band`, `prior_claims`, `calculate_risk_score`, `issue_binder`, `lookup_policy`, `research_subagent`, `product` (as insurance product type) |
