"""Offline scenarios — fully deterministic runs with zero keys / zero infra.

Each scenario supplies (a) the scripted model turns the MockProvider replays and
(b) a MockContext that mocks the connectors/MCP the package would otherwise hit
live. This is what powers the CLI `demo` subcommand and the notebook: the exact
same engine the worker runs, driven by the mock seams instead of real vendors.

Because the MockProvider serves ALL model calls in call order (including a
sub-agent's), the underwriting scenario interleaves parent and child turns in
the order the loop will request them.
"""

from __future__ import annotations

from harness.mock.context import MockContext
from harness.providers.mock_provider import ScriptedTurn, ScriptedToolCall


# ── classify_document (task) ──
def classify_scenario():
    turns = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(
            name="structured_output",
            input={"category": "complaint", "confidence": 0.88,
                   "rationale": "Customer expresses dissatisfaction and requests escalation."})]),
    ]
    mock = MockContext(suppress_targets=True)  # no S3 offline
    inp = {"text": "I have been waiting three weeks for a callback and I am furious. Escalate this now."}
    return ("classify_document", "task", inp, turns, mock)


# ── underwriting_agent (agent) — full path incl. delegation + HITL ──
def underwriting_scenario():
    # Call order the loop will produce:
    #   parent turn0: delegate_to_agent(research_subagent)
    #     child  turn0: search(...)
    #     child  turn1: submit_output(findings)
    #   parent turn1: lookup_policy(...)
    #   parent turn2: calculate_risk_score(...)
    #   parent turn3: issue_binder(...)   ← HITL GATE fires here (suspend)
    #   [resume] parent turn4: submit_output(decision)
    parent_pre = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="delegate_to_agent",
                     input={"agent_name": "research_subagent",
                            "context": {"topic": "auto insurance risk for applicant 1"}})]),
    ]
    child = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="search", input={"query": "auto risk applicant 1"})]),
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="submit_output",
                     input={"findings": "Region shows average loss ratios; no red flags.", "sources_count": 3})]),
    ]
    parent_mid = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="lookup_policy", input={"product": "auto"})]),
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="calculate_risk_score",
                     input={"age": 41, "risk_band": "medium", "prior_claims": 1})]),
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="issue_binder",
                     input={"applicant_id": 1, "premium": 1240.0})]),
    ]
    parent_post = [  # served after resume
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="submit_output",
                     input={"decision": "approve", "premium": 1240.0, "risk_score": 42.2,
                            "rationale": "Medium band, single prior claim, policy rules permit binding."})]),
    ]
    turns = parent_pre + child + parent_mid + parent_post
    mock = MockContext(
        source_responses={"applicant": [{"id": 1, "name": "Dana Lee", "age": 41,
                                          "risk_band": "medium", "prior_claims": 1}]},
        tool_responses={
            "lookup_policy": {"content": [{"type": "text", "text": "auto: max_premium 5000, min_age 18"}],
                              "is_error": False, "text": "auto: max_premium 5000, min_age 18"},
            "search": {"content": [{"type": "text", "text": "3 results"}], "is_error": False, "text": "3 results"},
        },
        suppress_targets=True,
    )
    inp = {"applicant_id": 1, "product": "auto"}
    return ("underwriting_agent", "agent", inp, turns, mock)


SCENARIOS = {
    "classify": classify_scenario,
    "underwriting": underwriting_scenario,
}


__all__ = ["SCENARIOS", "classify_scenario", "underwriting_scenario"]
