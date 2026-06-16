# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end offline smoke test — zero API keys, zero external infra.

Exercises every load-bearing path through the engine using only the mock
provider and mocked connectors:
  1. run_task with forced structured output + a suppressed S3 target
  2. run_agent with a python tool call
  3. run_agent that delegates to a sub-agent
  4. run_agent that hits a HITL gate → suspends → resumes on approval
"""

import asyncio
import tempfile
from uuid import uuid4

from harness.core.package import (
    Package, EntityKind, InferenceConfig, ModelRef, ToolAuthorization,
    TargetBinding, DelegationAuthorization, HITLPolicy,
)
from harness.core.engine import ExecutionEngine
from harness.core.trace import Tracer
from harness.core.result import RunStatus, HITLSuspended
from harness.providers.base import ModelChain
from harness.providers.mock_provider import MockProvider, ScriptedTurn, ScriptedToolCall
from harness.tools.gateway import ToolGateway
from harness.tools.python_tools import PythonToolRegistry
from harness.connectors.base import ConnectorRegistry
from harness.connectors.binder import Binder
from harness.decisions.assembler import FileSink
from harness.hitl.continuation import FileContinuationStore, HumanDecision
from harness.mock.context import MockContext

TMP = tempfile.mkdtemp()


def chain(): return [ModelRef(provider="mock", model="mock-1", priority=0)]


def make_engine(turns, packages, python_tools=None):
    providers = {"mock": MockProvider(turns=turns)}
    gateway = ToolGateway(python_tools=python_tools or PythonToolRegistry())
    binder = Binder(ConnectorRegistry())
    return ExecutionEngine(
        chain=ModelChain(providers), tool_gateway=gateway, binder=binder,
        packages={p.name: p for p in packages},
        sink=FileSink(TMP), continuation_store=FileContinuationStore(f"{TMP}/susp"),
        tracer=Tracer(enabled=False),
    )


async def test_task():
    pkg = Package(name="classify", kind=EntityKind.TASK, inference=InferenceConfig(chain=chain()),
                  prompt_template="Classify: {{input.text}}",
                  output_schema={"label": {"type": "string"}, "confidence": {"type": "number"}},
                  targets=[TargetBinding(connector="s3_main", method="put_object",
                                         from_path="$", container="out/{{run_id}}.json", required=False)])
    turns = [ScriptedTurn(tool_calls=[ScriptedToolCall(name="structured_output",
                                                       input={"label": "spam", "confidence": 0.97})])]
    eng = make_engine(turns, [pkg])
    # suppress the S3 target so we don't need MinIO offline
    res = await eng.run_task(task_name="classify", input_data={"text": "win a prize"},
                             mock=MockContext(suppress_targets=True))
    assert res.status == RunStatus.COMPLETE, res
    assert res.output == {"label": "spam", "confidence": 0.97}, res.output
    print(f"  [task] {res.output}  decision={res.decision_log_id}")


async def test_agent_tool():
    pkg = Package(name="calc", kind=EntityKind.AGENT, inference=InferenceConfig(chain=chain(), max_turns=5),
                  tools=[ToolAuthorization(name="add", transport="python_inprocess",
                                           input_schema={"type": "object",
                                                         "properties": {"a": {"type": "number"},
                                                                        "b": {"type": "number"}}})])
    turns = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="add", input={"a": 2, "b": 3})]),
        ScriptedTurn(text='{"answer": 5}'),
    ]
    tools = PythonToolRegistry()
    tools.register("add", lambda a, b: {"sum": a + b})
    eng = make_engine(turns, [pkg], python_tools=tools)
    res = await eng.run_agent(agent_name="calc", context={"q": "2+3"})
    assert res.status == RunStatus.COMPLETE, res
    assert res.output == {"answer": 5}, res.output
    # the tool call must be recorded
    print(f"  [agent+tool] {res.output}")


async def test_delegation():
    parent = Package(name="orchestrator", kind=EntityKind.AGENT,
                     inference=InferenceConfig(chain=chain(), max_turns=5),
                     tools=[ToolAuthorization(name="delegate_to_agent", transport="verity_builtin",
                                              input_schema={"type": "object",
                                                            "properties": {"agent_name": {"type": "string"},
                                                                           "context": {"type": "object"}}})],
                     delegations=[DelegationAuthorization(child_agent="researcher")])
    child = Package(name="researcher", kind=EntityKind.AGENT,
                    inference=InferenceConfig(chain=chain(), max_turns=3))
    # parent: turn0 delegates; turn1 (after sub result) finishes.
    # child: turn0 finishes.
    parent_turns = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="delegate_to_agent",
                                                  input={"agent_name": "researcher",
                                                         "context": {"topic": "rates"}})]),
        ScriptedTurn(text='{"final": "done with sub help"}'),
    ]
    child_turns = [ScriptedTurn(text='{"findings": "rates are rising"}')]
    # one mock provider can't serve two independent scripts; give the chain a
    # provider whose script is parent then child concatenated in call order.
    providers = {"mock": MockProvider(turns=parent_turns[:1] + child_turns + parent_turns[1:])}
    gateway = ToolGateway(python_tools=PythonToolRegistry())
    eng = ExecutionEngine(chain=ModelChain(providers), tool_gateway=gateway,
                          binder=Binder(ConnectorRegistry()),
                          packages={parent.name: parent, child.name: child},
                          sink=FileSink(TMP), continuation_store=FileContinuationStore(f"{TMP}/susp"),
                          tracer=Tracer(enabled=False))
    res = await eng.run_agent(agent_name="orchestrator", context={"goal": "research rates"})
    assert res.status == RunStatus.COMPLETE, res
    assert res.output == {"final": "done with sub help"}, res.output
    print(f"  [delegation] {res.output}")


async def test_hitl():
    pkg = Package(name="payer", kind=EntityKind.AGENT, inference=InferenceConfig(chain=chain(), max_turns=5),
                  tools=[ToolAuthorization(name="issue_payment", transport="python_inprocess",
                                           input_schema={"type": "object",
                                                         "properties": {"amount": {"type": "number"}}})],
                  hitl=HITLPolicy(enabled=True, require_approval_for=["issue_payment"]))
    turns = [
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="issue_payment", input={"amount": 5000})]),
        ScriptedTurn(text='{"status": "paid"}'),
    ]
    tools = PythonToolRegistry()
    tools.register("issue_payment", lambda amount: {"paid": amount, "ref": "PAY-1"})
    eng = make_engine(turns, [pkg], python_tools=tools)

    suspension_id = None
    try:
        await eng.run_agent(agent_name="payer", context={"invoice": 42})
        assert False, "should have suspended"
    except HITLSuspended as s:
        suspension_id = s.suspension_id
        print(f"  [hitl] suspended at gate {s.gate_tool!r} → {suspension_id}")

    # human approves, then resume
    await eng.continuations.record_decision(
        suspension_id, HumanDecision(decision="approve", decided_by="anil"))
    res = await eng.resume(suspension_id)
    assert res.status == RunStatus.COMPLETE, res
    assert res.output == {"status": "paid"}, res.output
    print(f"  [hitl] resumed & completed → {res.output}")


async def main():
    print("offline smoke test:")
    await test_task()
    await test_agent_tool()
    await test_delegation()
    await test_hitl()
    print("ALL PASSED ✓")
    print("artifacts under:", TMP)


if __name__ == "__main__":
    asyncio.run(main())
