# Appendix 03 — COPE Underwriting: Rating, Binding, and Referral

This appendix explains the COPE rating framework used by the `underwriting_agent`
demo, how the harness applies it, and what the two demo scenarios — one that binds
and one that refers — look like end to end.

---

## What is COPE?

COPE is the standard commercial property underwriting framework. The acronym names
the four inputs that drive the base rate calculation:

| Letter | Factor | What it captures |
|---|---|---|
| **C** | Construction | How the building is built — frame burns faster than masonry |
| **O** | Occupancy | What the building is used for — a restaurant carries more fire risk than an office |
| **P** | Protection | Proximity and quality of fire response — Protection Class 1 (best) to 10 (worst) |
| **E** | Exposure | External hazards — wind zone, flood zone, distance to coast, prior catastrophe |

The output of a COPE rating is an **annual premium**. The formula is:

```
premium = TIV × base_rate × construction_factor × protection_factor
                           × sprinkler_credit × deductible_credit
```

`TIV` (Total Insured Value) is the dollar amount being insured. Everything else is
a multiplier derived from the COPE factors and property features.

---

## How the harness implements it

The `rate_property` python tool in [`scripts/demo_app.py`](../scripts/demo_app.py)
holds the factor tables. These are illustrative but domain-credible — not
production tariffs.

**Occupancy base rates** (annual rate as % of TIV):

| Occupancy | Base rate | Rationale |
|---|---|---|
| Restaurant | 0.45% | Elevated fire/cooking risk |
| Warehouse | 0.55% | Storage and vacancy risk |
| Retail | 0.38% | |
| Office | 0.25% | Low hazard |

**Construction multipliers** (applied to base rate):

| Construction type | Multiplier | Effect |
|---|---|---|
| Frame | 1.35 | +35% — burns readily |
| Joisted masonry | 1.10 | +10% — wood floors/roof, masonry walls |
| Masonry | 1.00 | baseline |
| Non-combustible | 0.90 | −10% credit |
| Fire resistive | 0.80 | −20% credit |

**Protection class factors:**

| PPC range | Factor | Interpretation |
|---|---|---|
| 1–3 | 0.80 | Excellent fire response, −20% |
| 4–6 | 0.95 | Good, −5% |
| 7–8 | 1.20 | Below average, +20% |
| 9–10 | 1.50 | Poor, +50% |

**Additional credits:**

| Feature | Factor |
|---|---|
| Fully sprinklered | 0.90 (−10%) |
| Not sprinklered | 1.00 (no change) |
| $1,000 deductible | 1.00 |
| $5,000 deductible | 0.95 |
| $10,000 deductible | 0.88 |
| $25,000 deductible | 0.80 |

---

## Binding vs. referral

After rating, the agent applies five underwriting checks. **All five must pass** to
bind. If any fail, the risk is referred to a senior underwriter instead.

| Check | Threshold | What triggers it |
|---|---|---|
| In appetite | `in_appetite == True` | Risk type / construction / state outside carrier guidelines |
| TIV authority | `TIV ≤ authority_tiv` | Too large for this underwriter to bind unilaterally |
| Premium authority | `computed_premium ≤ authority_premium` | Premium too high for delegated authority |
| Loss ratio | `loss_ratio < 0.60` | Poor claims history relative to earned premium |
| Wind/cat exposure | `wind_zone == "none"` OR `distance_to_coast > 25 mi` | Hurricane-exposed property |

**Bind** — all checks pass. The agent calls `bind_policy`, which is a HITL gate:
the run suspends until a human underwriter approves. On approval the binder number
is issued and the decision is written to S3.

**Refer** — one or more checks fail. The agent calls `submit_output` directly with
`decision="refer"` and lists every failed check in `referral_reasons`. No
`bind_policy` call is made; nothing is written to the policy system.

**Decline** — used when `in_appetite == False` (risk type the carrier won't write
at all, not merely outside authority). The output_schema accepts `decline` as a
distinct value from `refer`.

---

## Example 1 — Lakeview Bistro LLC (bind)

**Submission** (from `submission` table, id=1):

| Field | Value |
|---|---|
| Named insured | Lakeview Bistro LLC |
| Address | 221 Lakeview Ave, Chicago, IL |
| Occupancy | Restaurant |
| Construction | Joisted masonry |
| Year built | 1998 |
| TIV / Limit | $1,850,000 |
| Deductible | $5,000 |
| Sprinklered | Yes |
| Protection class | 4 |

**Step 1 — Loss history** (delegated to `loss_history_analyst`):

| Metric | Value |
|---|---|
| Claims (5yr) | 1 (2023 kitchen fire, $45K, closed) |
| Incurred total | $45,000 |
| Earned premium | $300,000 |
| Loss ratio | **0.15** |
| Large losses (>$50K) | None |

**Step 2 — Appetite check** (MCP `lookup_appetite`):

- In appetite: **yes**
- Authority TIV: $3,000,000
- Authority premium: $15,000
- Referral triggers: none

**Step 3 — Property enrichment** (MCP `property_data`):

- Wind zone: **none**
- Flood zone: X (minimal)
- Distance to coast: 800 mi
- Prior cat: no
- Protection class confirmed: 4, Chicago FD

**Step 4 — COPE rating** (`rate_property`):

```
premium = $1,850,000
          × 0.0045   (restaurant base rate)
          × 1.10     (joisted masonry)
          × 0.95     (PPC 4)
          × 0.90     (sprinkler credit)
          × 0.95     (deductible $5K)
        = $7,438.18
```

| Factor | Value |
|---|---|
| Base rate | 0.0045 |
| Construction | 1.10 |
| Protection class | 0.95 |
| Sprinkler credit | 0.90 |
| Deductible credit | 0.95 |
| **Annual premium** | **$7,438.18** |

**Step 5 — Checks:**

| Check | Result |
|---|---|
| In appetite | ✓ yes |
| TIV $1.85M ≤ authority $3.0M | ✓ pass |
| Premium $7,438 ≤ authority $15,000 | ✓ pass |
| Loss ratio 0.15 < 0.60 | ✓ pass |
| Wind zone "none" | ✓ pass |

All five pass. The agent calls `bind_policy` → HITL gate fires → run suspends.
After human approval the run resumes and `submit_output` returns:

```json
{
  "decision": "bound",
  "premium": 7438.18,
  "referral_reasons": [],
  "binder_number": "BND-0001-145112"
}
```

---

## Example 2 — Gulfside Storage Inc (refer)

**Submission** (from `submission` table, id=2):

| Field | Value |
|---|---|
| Named insured | Gulfside Storage Inc |
| Address | 9 Harbor Rd, Tampa, FL |
| Occupancy | Warehouse |
| Construction | Frame |
| Year built | 1975 |
| TIV / Limit | $8,000,000 |
| Deductible | $10,000 |
| Sprinklered | No |
| Protection class | 8 |

**Step 1 — Loss history:**

| Metric | Value |
|---|---|
| Claims (5yr) | 4 |
| Hurricane Ian 2022 | $600,000 (closed) |
| Wind/hail 2022 | $95,000 (closed) |
| Water intrusion 2023 | $85,000 (closed) |
| Vandalism 2024 | $40,000 (closed) |
| Incurred total | $820,000 |
| Earned premium | $1,000,000 |
| Loss ratio | **0.82** |

**Step 2 — Appetite check:**

- In appetite: yes (but with referral triggers flagged)
- Authority TIV: **$5,000,000**
- Authority premium: **$25,000**
- Referral triggers: `frame_construction_fl`, `wind_zone_high`, `cat_exposed`

**Step 3 — Property enrichment:**

- Wind zone: **high**
- Flood zone: AE (high-risk flood area)
- Distance to coast: **3 mi**
- Prior cat: **yes**
- Protection class confirmed: 8, Hillsborough County FD

**Step 4 — COPE rating:**

```
premium = $8,000,000
          × 0.0055   (warehouse base rate)
          × 1.35     (frame)
          × 1.20     (PPC 8)
          × 1.00     (not sprinklered)
          × 0.88     (deductible $10K)
        = $62,726.40
```

| Factor | Value |
|---|---|
| Base rate | 0.0055 |
| Construction | 1.35 |
| Protection class | 1.20 |
| Sprinkler credit | 1.00 (no credit) |
| Deductible credit | 0.88 |
| **Annual premium** | **$62,726.40** |

**Step 5 — Checks:**

| Check | Result |
|---|---|
| In appetite | ✓ (technically yes, but with triggers) |
| TIV $8.0M ≤ authority $5.0M | ✗ **exceeds by $3M** |
| Premium $62,726 ≤ authority $25,000 | ✗ **exceeds by $37K** |
| Loss ratio 0.82 < 0.60 | ✗ **82% loss ratio** |
| Wind zone "high", coast 3 mi ≤ 25 mi | ✗ **cat-exposed** |

Four checks fail. The agent skips `bind_policy` entirely and calls `submit_output`
directly:

```json
{
  "decision": "refer",
  "premium": 62726.40,
  "referral_reasons": [
    "TIV $8,000,000 exceeds authority limit $5,000,000",
    "Computed premium $62,726.40 exceeds authority limit $25,000",
    "Loss ratio 0.82 exceeds threshold 0.60",
    "Wind zone high with distance to coast 3 mi (threshold: >25 mi)"
  ]
}
```

No HITL gate fires. No binder is issued. No S3 write occurs. The result goes
straight back to the calling application for routing to a senior underwriter.

---

## How this maps to harness components

```
Submission (read in from Postgres)
    │
    ├─► delegate_to_agent ──► loss_history_analyst
    │                             └─► pull_loss_runs (MCP policy_service)
    │
    ├─► lookup_appetite (MCP policy_service)   ─┐ parallel
    ├─► property_data   (MCP policy_service)   ─┘
    │
    ├─► rate_property (python_inprocess)
    │
    └─► [all checks pass?]
          YES → bind_policy (python_inprocess, HITL gate)
                    └─► S3 target write
          NO  → submit_output directly (no gate, no write)
```

The five underwriting checks are encoded in the agent's system prompt
(`underwriting_agent.agent.yaml`) — not in harness logic. The harness enforces the
HITL gate (`require_approval_for: [bind_policy]`) and the output schema
(`decision` must be one of `bound / quote / refer / decline`), but the
underwriting judgement lives entirely in the package.
