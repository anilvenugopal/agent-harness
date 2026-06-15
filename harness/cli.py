"""Harness CLI.

Subcommands:
  demo <scenario> [--step] [--auto-approve]
        Fully OFFLINE scripted run (zero keys/infra) with the Rich tracer.
        --step pauses after each stage so you can single-step the loop;
        set a VSCode breakpoint in harness/core/trace.py:Tracer.emit or in any
        gateway to stop with full neutral state in scope.

  run <package> --input '{...}' [--step] [--no-trace]
        LIVE run using whatever providers have API keys in the env, plus real
        connectors/MCP from the env. (Falls back through the model chain.)

  enqueue <package> --input '{...}'      Insert a queued run into Postgres.
  worker [--worker-id w1]                Start a Postgres SKIP LOCKED worker.
  list-suspensions                       Show runs awaiting a human decision.
  resume <suspension_id> --decision approve|deny|edit [--note ...]
  reproduce <run_id>                     Replay a past run from its recorded
                                         model_invocations.jsonl (audit replay).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from uuid import UUID

from harness.core.engine import ExecutionEngine
from harness.core.factory import (
    build_connectors, build_engine, build_providers, load_packages,
)
from harness.core.trace import Tracer
from harness.connectors.binder import Binder
from harness.connectors.base import ConnectorRegistry
from harness.decisions.assembler import FileSink
from harness.hitl.continuation import FileContinuationStore, HumanDecision
from harness.mcp.client import MCPClient
from harness.providers.base import ModelChain
from harness.providers.mock_provider import MockProvider
from harness.tools.gateway import ToolGateway

ARTIFACTS = os.environ.get("ARTIFACTS_ROOT", "./_artifacts")
PACKAGE_DIR = os.environ.get("PACKAGE_DIR", "packages")


def _mcp_servers() -> dict:
    """policy_service over Streamable HTTP if MCP_POLICY_URL is set, else stdio."""
    url = os.environ.get("MCP_POLICY_URL")
    if url:
        return {"policy_service": {"name": "policy_service", "transport": "streamable_http", "url": url}}
    return {"policy_service": {"name": "policy_service", "transport": "stdio",
                               "command": "python", "args": ["mcp_servers/example_server.py", "stdio"]}}


# ──────────────────────────────────────────────────────────────────────
# demo (offline)
# ──────────────────────────────────────────────────────────────────────
async def cmd_demo(args):
    from scripts.scenarios import SCENARIOS
    from scripts.demo_app import build_demo_tools

    if args.scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario {args.scenario!r}. Known: {sorted(SCENARIOS)}")
    pkg_name, kind, inp, turns, mock = SCENARIOS[args.scenario]()

    packages = load_packages(PACKAGE_DIR)
    # Offline: one shared MockProvider serves EVERY chain link (so the call
    # counter is shared across providers and across delegated sub-agents). The
    # package's real provider names (anthropic/openai/gemini) are aliased to it,
    # so the chain resolves link 0 to the mock without rewriting the package.
    shared_mock = MockProvider(turns=turns)
    providers = {name: shared_mock for name in ("anthropic", "openai", "gemini", "mock")}
    engine = ExecutionEngine(
        chain=ModelChain(providers),
        tool_gateway=ToolGateway(python_tools=build_demo_tools()),
        binder=Binder(ConnectorRegistry()),     # connectors mocked via MockContext
        packages=packages,
        sink=FileSink(ARTIFACTS),
        continuation_store=FileContinuationStore(f"{ARTIFACTS}/suspensions"),
        tracer=Tracer(enabled=True, step=args.step),
    )

    from harness.core.result import HITLSuspended, RunStatus
    try:
        if kind == "task":
            res = await engine.run_task(task_name=pkg_name, input_data=inp, mock=mock)
        else:
            res = await engine.run_agent(agent_name=pkg_name, context=inp, mock=mock)
        _print_result(res)
    except HITLSuspended as s:
        print(f"\n⏸  SUSPENDED for human approval: gate={s.gate_tool!r} suspension={s.suspension_id}")
        if args.auto_approve:
            print("   --auto-approve: recording approval and resuming...\n")
            await engine.continuations.record_decision(
                s.suspension_id, HumanDecision(decision="approve", decided_by="demo"))
            res = await engine.resume(s.suspension_id)
            _print_result(res)
        else:
            print(f"   resume with: python -m harness.cli resume {s.suspension_id} --decision approve")


# ──────────────────────────────────────────────────────────────────────
# run (live)
# ──────────────────────────────────────────────────────────────────────
async def cmd_run(args):
    from scripts.demo_app import build_demo_tools
    packages = load_packages(PACKAGE_DIR)
    providers = build_providers()
    if len(providers) == 1:  # only mock
        print("⚠  no provider API keys found in env; only the mock provider is available.")
        print("   set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY, or use `demo`.")
    engine = build_engine(
        packages=packages, providers=providers, connectors=build_connectors(),
        python_tools=build_demo_tools(), mcp_client=MCPClient(_mcp_servers()),
        sink=FileSink(ARTIFACTS),
        continuation_store=FileContinuationStore(f"{ARTIFACTS}/suspensions"),
        tracer=Tracer(enabled=not args.no_trace, step=args.step),
    )
    pkg = packages[args.package]
    inp = json.loads(args.input)
    from harness.core.result import HITLSuspended
    try:
        if pkg.kind.value == "task":
            res = await engine.run_task(task_name=args.package, input_data=inp)
        else:
            res = await engine.run_agent(agent_name=args.package, context=inp)
        _print_result(res)
    except HITLSuspended as s:
        print(f"\n⏸  SUSPENDED: {s.suspension_id} (gate {s.gate_tool!r})")
    finally:
        if engine.tools.mcp_client:
            await engine.tools.mcp_client.close_all()


# ──────────────────────────────────────────────────────────────────────
# reproduce (audit replay)
# ──────────────────────────────────────────────────────────────────────
async def cmd_reproduce(args):
    from scripts.demo_app import build_demo_tools
    from harness.mock.context import MockContext
    packages = load_packages(PACKAGE_DIR)
    sink = FileSink(ARTIFACTS)
    recorded = sink.load_model_invocations(args.run_id)
    print(f"replaying {len(recorded)} recorded model turn(s) for run {args.run_id}")

    # Load the original decision to recover entity + input.
    import glob
    dec = None
    for p in glob.glob(f"{ARTIFACTS}/runs/**/{args.run_id}/decision_log.json", recursive=True):
        dec = json.load(open(p)); break
    if dec is None:
        raise SystemExit(f"no decision_log.json found for run {args.run_id}")

    shared_replay = MockProvider(replay=recorded)
    providers = {name: shared_replay for name in ("anthropic", "openai", "gemini", "mock")}
    engine = ExecutionEngine(
        chain=ModelChain(providers), tool_gateway=ToolGateway(python_tools=build_demo_tools()),
        binder=Binder(ConnectorRegistry()), packages=packages, sink=sink,
        tracer=Tracer(enabled=True),
    )
    # Replay everything mocked: model from recording, all tools/sources canned, targets suppressed.
    mock = MockContext(model_replay=recorded, mock_all_tools=True, suppress_targets=True)
    if dec["entity_kind"] == "task":
        res = await engine.run_task(task_name=dec["entity_name"], input_data=dec["input_json"], mock=mock)
    else:
        res = await engine.run_agent(agent_name=dec["entity_name"], context=dec["input_json"], mock=mock)
    print(f"\nreproduced status={res.status.value}; new decision={res.decision_log_id}")
    _print_result(res)


# ──────────────────────────────────────────────────────────────────────
# postgres-backed worker-mode commands
# ──────────────────────────────────────────────────────────────────────
async def cmd_enqueue(args):
    from harness.worker.worker import enqueue_run
    dsn = _require_dsn()
    pkg = load_packages(PACKAGE_DIR)[args.package]
    rid = await enqueue_run(dsn, entity_kind=pkg.kind.value, entity_name=args.package,
                            input_data=json.loads(args.input))
    print(f"enqueued run {rid} ({pkg.kind.value} {args.package})")


async def cmd_worker(args):
    from scripts.demo_app import build_demo_tools
    from harness.worker.worker import Worker
    dsn = _require_dsn()
    packages = load_packages(PACKAGE_DIR)
    engine = build_engine(
        packages=packages, providers=build_providers(), connectors=build_connectors(),
        python_tools=build_demo_tools(), mcp_client=MCPClient(_mcp_servers()),
        sink=FileSink(ARTIFACTS),
        continuation_store=FileContinuationStore(f"{ARTIFACTS}/suspensions"),
        tracer=Tracer(enabled=not args.no_trace),
    )
    await Worker(engine, dsn, worker_id=args.worker_id).run_forever()


async def cmd_list_suspensions(args):
    store = FileContinuationStore(f"{ARTIFACTS}/suspensions")
    pending = await store.list_pending()
    if not pending:
        print("no pending suspensions.")
        return
    for c in pending:
        print(f"{c.id}  run={c.run_id}  agent={c.package_name}  gate={c.gate_tool}  input={c.pending_tool_use['input']}")


async def cmd_resume(args):
    from scripts.demo_app import build_demo_tools
    store = FileContinuationStore(f"{ARTIFACTS}/suspensions")
    decision = HumanDecision(decision=args.decision, note=args.note,
                             edited_input=json.loads(args.edited_input) if args.edited_input else None,
                             decided_by=args.by)
    await store.record_decision(UUID(args.suspension_id), decision)

    # build a live engine to continue the run (real providers/tools)
    packages = load_packages(PACKAGE_DIR)
    engine = build_engine(
        packages=packages, providers=build_providers(), connectors=build_connectors(),
        python_tools=build_demo_tools(), mcp_client=MCPClient(_mcp_servers()),
        sink=FileSink(ARTIFACTS), continuation_store=store, tracer=Tracer(enabled=True),
    )
    res = await engine.resume(UUID(args.suspension_id))
    # if a Postgres run row exists, flip it back so the worker re-claimed it;
    # here we resumed inline, so just report.
    _print_result(res)


# ── helpers ──
def _require_dsn() -> str:
    dsn = os.environ.get("PG_MAIN_DSN") or os.environ.get("DECISION_PG_DSN")
    if not dsn:
        raise SystemExit("set PG_MAIN_DSN to use worker-mode commands.")
    return dsn


def _print_result(res):
    print("\n── result ──")
    print(f"status     : {res.status.value}")
    print(f"entity     : {res.entity_kind} {res.entity_name}")
    print(f"decision   : {res.decision_log_id}")
    print(f"tokens     : in={res.usage.input_tokens} out={res.usage.output_tokens}")
    print(f"duration_ms: {res.duration_ms}")
    print(f"output     : {json.dumps(res.output, indent=2, default=str)}")
    if res.error_message:
        print(f"error      : {res.error_message}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Multi-provider agent harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="offline scripted run (no keys/infra)")
    d.add_argument("scenario", help="classify | underwriting")
    d.add_argument("--step", action="store_true", help="pause after each stage")
    d.add_argument("--auto-approve", action="store_true", help="auto-approve HITL gates and resume")
    d.set_defaults(fn=cmd_demo)

    r = sub.add_parser("run", help="live run using env API keys")
    r.add_argument("package")
    r.add_argument("--input", required=True, help="JSON input")
    r.add_argument("--step", action="store_true")
    r.add_argument("--no-trace", action="store_true")
    r.set_defaults(fn=cmd_run)

    rep = sub.add_parser("reproduce", help="replay a past run from its recording")
    rep.add_argument("run_id")
    rep.set_defaults(fn=cmd_reproduce)

    e = sub.add_parser("enqueue", help="insert a queued run (Postgres)")
    e.add_argument("package"); e.add_argument("--input", required=True)
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("worker", help="start a Postgres SKIP LOCKED worker")
    w.add_argument("--worker-id", default="worker-1"); w.add_argument("--no-trace", action="store_true")
    w.set_defaults(fn=cmd_worker)

    ls = sub.add_parser("list-suspensions", help="show pending HITL suspensions")
    ls.set_defaults(fn=cmd_list_suspensions)

    rs = sub.add_parser("resume", help="record a human decision and continue a suspended run")
    rs.add_argument("suspension_id")
    rs.add_argument("--decision", required=True, choices=["approve", "deny", "edit"])
    rs.add_argument("--edited-input", help="JSON (for --decision edit)")
    rs.add_argument("--note"); rs.add_argument("--by", default="cli-user")
    rs.set_defaults(fn=cmd_resume)
    return p


def main():
    args = build_parser().parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
