# demo_app.py — python_inprocess tools for the underwriting_agent demo.
#
# rate_property: COPE rating engine (Construction, Occupancy, Protection, Exposure).
#   Factor tables are illustrative but domain-credible; they are NOT production tariffs.
#
# bind_policy: issues a stub binder number. In production this would write to the
#   policy admin system via an API call; here it just stamps a reference ID so the
#   demo generates a tangible artefact.

from __future__ import annotations

import random
import string

from harness.tools.python_tools import PythonToolRegistry

# ── COPE factor tables ────────────────────────────────────────────────────────

_BASE_RATES: dict[str, float] = {
    "restaurant":  0.0045,   # 0.45% — elevated fire/liability risk
    "warehouse":   0.0055,   # 0.55% — storage & vacancy risk
    "office":      0.0025,   # 0.25%
    "retail":      0.0038,   # 0.38%
    "other":       0.0035,   # 0.35%
}

_CONSTRUCTION_FACTORS: dict[str, float] = {
    "frame":            1.35,
    "joisted_masonry":  1.10,
    "masonry":          1.00,
    "non_combustible":  0.90,
    "fire_resistive":   0.80,
}

_DEDUCTIBLE_CREDITS: dict[int, float] = {
    1_000:  1.00,
    5_000:  0.95,
    10_000: 0.88,
    25_000: 0.80,
}

_SPRINKLER_CREDIT = 0.90   # 10% discount when fully sprinklered


def _ppc_factor(ppc: int) -> float:
    """Protection class loading: better protection (lower PPC) = lower factor."""
    if ppc <= 3:
        return 0.80
    if ppc <= 6:
        return 0.95
    if ppc <= 8:
        return 1.20
    return 1.50


def _nearest_deductible_credit(deductible: int) -> float:
    tiers = sorted(_DEDUCTIBLE_CREDITS)
    chosen = min(tiers, key=lambda t: abs(t - deductible))
    return _DEDUCTIBLE_CREDITS[chosen]


# ── in-process tool functions ─────────────────────────────────────────────────

def rate_property(
    tiv: float,
    occupancy: str,
    construction: str,
    protection_class: int,
    sprinklered: bool,
    deductible: int,
) -> dict:
    """Compute an annual premium via the COPE framework."""
    base       = _BASE_RATES.get(occupancy.lower(), _BASE_RATES["other"])
    c_factor   = _CONSTRUCTION_FACTORS.get(construction.lower(), 1.00)
    p_factor   = _ppc_factor(protection_class)
    spk_factor = _SPRINKLER_CREDIT if sprinklered else 1.00
    ded_factor = _nearest_deductible_credit(deductible)

    premium = tiv * base * c_factor * p_factor * spk_factor * ded_factor
    return {
        "premium": round(premium, 2),
        "cope_factors": {
            "base_rate":         round(base, 4),
            "construction":      c_factor,
            "protection_class":  p_factor,
            "sprinkler_credit":  spk_factor,
            "deductible_credit": ded_factor,
        },
    }


def bind_policy(
    submission_id: int,
    premium: float,
    limit: float,
    deductible: int,
) -> dict:
    """Stamp a binder number. HITL approval must occur before this executes."""
    suffix = "".join(random.choices(string.digits, k=6))
    binder_number = f"BND-{submission_id:04d}-{suffix}"
    return {
        "binder_number":  binder_number,
        "submission_id":  submission_id,
        "annual_premium": round(premium, 2),
        "coverage_limit": limit,
        "deductible":     deductible,
        "status":         "bound",
    }


# ── tool registry ─────────────────────────────────────────────────────────────

def build_demo_tools() -> PythonToolRegistry:
    """Return the python_inprocess tool map for the underwriting demo."""
    reg = PythonToolRegistry()
    reg.register("rate_property", rate_property)
    reg.register("bind_policy",   bind_policy)
    return reg
