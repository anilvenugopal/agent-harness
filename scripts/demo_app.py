"""Demo application wiring — the python tools the example packages call.

In Verity these would be the application's own tool implementations (Category B
python tools) registered into the worker at startup. Kept in one place so both
the CLI worker entrypoint and the offline scenarios register the same set.
"""

from __future__ import annotations

from harness.tools.python_tools import PythonToolRegistry


def calculate_risk_score(age: int, risk_band: str, prior_claims: int) -> dict:
    """A toy deterministic risk model (0-100)."""
    band = {"low": 10, "medium": 30, "high": 60}.get(str(risk_band).lower(), 40)
    score = band + min(prior_claims, 5) * 6 + max(0, (age - 25)) * 0.2
    score = max(0, min(100, round(score, 1)))
    return {"risk_score": score, "band": risk_band}


def issue_binder(applicant_id: int, premium: float) -> dict:
    """Issue a binder. This is the HITL-gated tool — by the time this runs, a
    human has already approved (or edited) the call via the continuation flow."""
    return {"binder_id": f"BND-{applicant_id}", "premium": premium, "issued": True}


def build_demo_tools() -> PythonToolRegistry:
    reg = PythonToolRegistry()
    reg.register("calculate_risk_score", calculate_risk_score)
    reg.register("issue_binder", issue_binder)
    return reg


__all__ = ["build_demo_tools", "calculate_risk_score", "issue_binder"]
