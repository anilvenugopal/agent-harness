# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interactive demo tool for agent-harness.

    python -m demo

Actions:
  status       Show Docker service health
  up           Start the Docker stack
  down         Stop the stack
  seed         Seed MinIO demo documents
  run          Execute a demo scenario
  logs         Tail a service log
  suspensions  Browse and action HITL suspensions
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from collections import namedtuple

# ── load .env and fix sys.path once at import time ──────────────────────────
_ROOT = pathlib.Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

try:
    from InquirerPy import inquirer
except ImportError:
    sys.exit("InquirerPy not installed — run: pip install InquirerPy")

from rich import box
from rich.console import Console

from demo._docker import SERVICES, compose_json, compose_stream
from demo._logs import find_run_suspensions, list_runs, print_decision_log, print_hitl, print_model_invocations

console = Console()

_ARTIFACTS  = _ROOT / "_artifacts"
_SUSPENSIONS = _ARTIFACTS / "suspensions"
_INPUTS     = pathlib.Path(__file__).parent / "inputs"

_RUN_OR_BACK = [
    {"name": "run ▶",          "value": "run"},
    {"name": "← back to menu", "value": "back"},
]

# ── scenarios ────────────────────────────────────────────────────────────────

_SCENARIOS = {
    "classify": {
        "desc":         "Classify a customer complaint document  [MinIO source — seed first]",
        "package":      "classify_document",
        "kind":         "task",
        "input_file":   "classify.json",
    },
    "underwriting_bind": {
        "desc":         "Underwrite Lakeview Bistro — expected outcome: bind",
        "package":      "underwriting_agent",
        "kind":         "agent",
        "input_file":   "underwriting_bind.json",
    },
    "underwriting_refer": {
        "desc":         "Underwrite Gulfside Storage — expected outcome: refer",
        "package":      "underwriting_agent",
        "kind":         "agent",
        "input_file":   "underwriting_refer.json",
    },
}

_RUN_OPTIONS = [
    {"name": "auto-approve    Auto-approve HITL gates without pausing", "value": "auto_approve"},
    {"name": "step            Pause after each model turn",             "value": "step"},
]

# ── engine builder ───────────────────────────────────────────────────────────

def _build_engine(*, step: bool = False):
    from harness.core.factory import (
        build_connectors, build_engine, build_providers, load_packages,
    )
    from harness.core.trace import Tracer
    from harness.decisions.assembler import FileSink
    from harness.hitl.continuation import FileContinuationStore
    from harness.mcp.client import MCPClient
    from scripts.demo_app import build_demo_tools

    mcp_url = os.environ.get("MCP_POLICY_URL")
    mcp_servers = (
        {"policy_service": {
            "name": "policy_service", "transport": "streamable_http", "url": mcp_url,
        }}
        if mcp_url else
        {"policy_service": {
            "name": "policy_service", "transport": "stdio",
            "command": "python", "args": [str(_ROOT / "mcp_servers" / "example_server.py"), "stdio"],
        }}
    )

    return build_engine(
        packages=load_packages(str(_ROOT / "packages")),
        providers=build_providers(),
        connectors=build_connectors(),
        python_tools=build_demo_tools(),
        mcp_client=MCPClient(mcp_servers),
        sink=FileSink(str(_ARTIFACTS)),
        continuation_store=FileContinuationStore(str(_SUSPENSIONS)),
        tracer=Tracer(enabled=True, step=step),
    )

# ── engine cleanup ───────────────────────────────────────────────────────────

async def _close_engine(engine) -> None:
    """Close MCP client and all provider HTTP clients before the event loop shuts down.

    Without this, httpx transports try to finalise after asyncio.run() closes the
    loop, producing 'Event loop is closed' noise even though the run succeeded.
    """
    import inspect
    if engine.tools.mcp_client:
        await engine.tools.mcp_client.close_all()
    for provider in engine.chain.providers.values():
        client = getattr(provider, "_client", None)
        if client:
            close = getattr(client, "close", None)
            if close and inspect.iscoroutinefunction(close):
                try:
                    await close()
                except Exception:
                    pass

# ── result display ───────────────────────────────────────────────────────────

def _print_result(res) -> None:
    color = "green" if res.status.value == "complete" else "red"
    console.print(f"\n[{color}]── result ──[/]")
    console.print(f"  status  : {res.status.value}")
    console.print(f"  tokens  : in={res.usage.input_tokens} out={res.usage.output_tokens}")
    console.print(f"  duration: {res.duration_ms}ms")
    if res.output:
        console.print(f"  output  :\n{json.dumps(res.output, indent=4)}")
    if res.error_message:
        console.print(f"  [red]error[/]   : {res.error_message}")

# ── actions ──────────────────────────────────────────────────────────────────

def status_action() -> None:
    rows = compose_json("ps", "--format", "json")
    if not rows:
        console.print("[dark_orange]No containers running.[/]")
        return
    for r in rows:
        name   = r.get("Service", r.get("Name", "?"))
        state  = r.get("State", "?")
        health = r.get("Health", "")
        ports  = r.get("Publishers", [])
        port_str = ""
        if isinstance(ports, list):
            port_str = "  ".join(
                f"{p['PublishedPort']}→{p['TargetPort']}"
                for p in ports if p.get("PublishedPort")
            )
        dot = "[green]●[/]" if state == "running" else "[red]●[/]"
        health_tag = f"[dim]({health})[/]" if health else ""
        console.print(f"  {dot} {name:20} {state:12} {health_tag:20} {port_str}")


def up_action() -> None:
    choice = inquirer.select(
        message="up — which services?",
        choices=[
            {"name": "infra only   postgres + minio + mcp-server", "value": "infra"},
            {"name": "full stack   + worker + jupyter",             "value": "full"},
            {"name": "← cancel",                                    "value": "cancel"},
        ],
    ).execute()
    if choice == "cancel":
        return
    services = [] if choice == "full" else ["postgres", "minio", "mcp-server"]
    console.print("[dim]Starting…[/]")
    for line in compose_stream("up", "-d", "--build", *services):
        console.print(f"[dim]{line}[/]")
    console.print("[green]Done.[/]")


def down_action() -> None:
    choice = inquirer.select(
        message="down — how?",
        choices=[
            {"name": "stop         Stop containers, keep volumes",  "value": "stop"},
            {"name": "wipe         Stop and delete all volumes",     "value": "wipe"},
            {"name": "← cancel",                                    "value": "cancel"},
        ],
    ).execute()
    if choice == "cancel":
        return
    args = ["down", "-v"] if choice == "wipe" else ["down"]
    for line in compose_stream(*args):
        console.print(f"[dim]{line}[/]")
    console.print("[green]Done.[/]")


def seed_action() -> None:
    choice = inquirer.select(
        message="seed — upload demo documents to MinIO",
        choices=[
            {"name": "seed         Upload samples/ to MinIO (idempotent)", "value": "seed"},
            {"name": "← cancel",                                           "value": "cancel"},
        ],
    ).execute()
    if choice == "cancel":
        return
    from scripts.seed_demo import main as _seed
    _seed()


def run_action() -> None:
    # step 1 — scenario
    scenario = inquirer.select(
        message="run — select scenario",
        choices=[
            {"name": f"{k:22} {v['desc']}", "value": k}
            for k, v in _SCENARIOS.items()
        ] + [{"name": "← back to menu", "value": "back"}],
    ).execute()
    if scenario == "back":
        return

    s = _SCENARIOS[scenario]
    inp = json.loads((_INPUTS / s["input_file"]).read_text())
    console.print(f"  [dim]package:[/] {s['package']}   [dim]input:[/] {json.dumps(inp)}")

    # step 2 — options
    opts = set(inquirer.checkbox(
        message="options  (space to toggle, enter to confirm)",
        choices=_RUN_OPTIONS,
    ).execute())

    # step 3 — run or back
    if inquirer.select(message="", choices=_RUN_OR_BACK).execute() == "back":
        return

    asyncio.run(_run_scenario(s, inp, auto_approve="auto_approve" in opts, step="step" in opts))


async def _run_scenario(s: dict, inp: dict, *, auto_approve: bool, step: bool) -> None:
    from harness.core.result import HITLSuspended
    from harness.hitl.continuation import HumanDecision

    engine = _build_engine(step=step)
    try:
        if s["kind"] == "task":
            res = await engine.run_task(task_name=s["package"], input_data=inp)
        else:
            res = await engine.run_agent(agent_name=s["package"], context=inp)
        _print_result(res)
    except HITLSuspended as exc:
        console.print(
            f"\n[dark_orange]⏸  Suspended[/]  gate=[bold]{exc.gate_tool}[/]"
            f"  suspension_id={exc.suspension_id}"
        )
        if auto_approve:
            console.print("  [dim]auto-approve: recording approval and resuming…[/]")
            await engine.continuations.record_decision(
                exc.suspension_id, HumanDecision(decision="approve", decided_by="demo"))
            _print_result(await engine.resume(exc.suspension_id))
        else:
            await _interactive_gate(engine, exc)
    finally:
        await _close_engine(engine)


async def _interactive_gate(engine, exc) -> None:
    from harness.hitl.continuation import HumanDecision

    action = await inquirer.select(
        message="HITL gate — choose action",
        choices=[
            {"name": "approve   Resume the run",              "value": "approve"},
            {"name": "deny      End the run with denial",     "value": "deny"},
            {"name": "save      Leave suspended, act later",  "value": "save"},
        ],
    ).execute_async()

    if action == "save":
        console.print(
            f"  Saved. Resume later:\n"
            f"  python -m harness.cli resume {exc.suspension_id} --decision approve"
        )
        return

    note = await inquirer.text(message="Note (optional, enter to skip):").execute_async() or None
    await engine.continuations.record_decision(
        exc.suspension_id, HumanDecision(decision=action, decided_by="demo", note=note))

    if action == "approve":
        _print_result(await engine.resume(exc.suspension_id))
    else:
        console.print("[red]Run denied.[/]")


def logs_action() -> None:
    service = inquirer.select(
        message="logs — select service",
        choices=[{"name": s, "value": s} for s in SERVICES]
               + [{"name": "← back", "value": "back"}],
    ).execute()
    if service == "back":
        return
    console.print(f"[dim]Tailing {service} — Ctrl+C to stop[/]")
    try:
        for line in compose_stream("logs", "--tail=50", "--follow", service):
            console.print(line)
    except KeyboardInterrupt:
        pass


def suspensions_action() -> None:
    _SUSPENSIONS.mkdir(parents=True, exist_ok=True)
    pending = []
    for p in sorted(_SUSPENSIONS.glob("*.json")):
        try:
            c = json.loads(p.read_text())
            if c.get("status") == "awaiting_decision":
                pending.append(c)
        except Exception:
            pass
    if not pending:
        console.print("[dim]No pending suspensions.[/]")
        return

    sid = inquirer.select(
        message=f"suspensions — {len(pending)} pending",
        choices=[
            {
                "name": (
                    f"{c['id'][:8]}…  "
                    f"gate={c['gate_tool']:15} "
                    f"pkg={c['package_name']:20} "
                    f"{c['created_at'][:19]}"
                ),
                "value": c["id"],
            }
            for c in pending
        ] + [{"name": "← back to menu", "value": "back"}],
    ).execute()
    if sid == "back":
        return

    cont = next(c for c in pending if c["id"] == sid)
    console.print(f"\n  gate       : [bold]{cont['gate_tool']}[/]")
    console.print(f"  package    : {cont['package_name']} {cont['package_version']}")
    console.print(f"  run_id     : {cont['run_id']}")
    console.print(f"  suspended  : {cont['created_at']}")
    console.print(f"  tool input : {json.dumps(cont['pending_tool_use'].get('input', {}), indent=2)}")

    action = inquirer.select(
        message="action",
        choices=[
            {"name": "approve   Resume the run",          "value": "approve"},
            {"name": "deny      End the run with denial", "value": "deny"},
            {"name": "← back    Take no action",          "value": "back"},
        ],
    ).execute()
    if action == "back":
        return

    note = inquirer.text(message="Note (optional, enter to skip):").execute() or None
    asyncio.run(_resolve_suspension(sid, action, note))


async def _resolve_suspension(sid: str, decision: str, note: str | None) -> None:
    from uuid import UUID
    from harness.hitl.continuation import HumanDecision

    engine = _build_engine()
    suspension_id = UUID(sid)
    await engine.continuations.record_decision(
        suspension_id, HumanDecision(decision=decision, decided_by="demo", note=note))

    try:
        if decision == "approve":
            _print_result(await engine.resume(suspension_id))
        else:
            console.print("[red]Run denied.[/]")
    finally:
        await _close_engine(engine)

# ── runs browser ─────────────────────────────────────────────────────────────

def runs_action() -> None:
    runs = list_runs(_ARTIFACTS)
    if not runs:
        console.print("[dim]No runs found in _artifacts/runs/.[/]")
        return

    status_icon = {"complete": "✓", "failed": "✗", "suspended": "⏸"}

    choices = []
    for r in runs:
        st    = r.get("status", "?")
        icon  = status_icon.get(st, "·")
        name  = r.get("entity_name", "?")[:24]
        ts    = r.get("created_at", "")[:16]
        ms    = r.get("duration_ms", 0)
        hitl  = "  [hitl]" if r.get("hitl_required") else ""
        depth = f"  depth={r['decision_depth']}" if r.get("decision_depth") else ""
        err   = f"  {r['error_message'][:55]}" if r.get("error_message") else ""
        label = f"{icon} {st:10} {name:26} {ts}  {ms}ms{hitl}{depth}{err}"
        choices.append({"name": label, "value": r["id"]})
    choices.append({"name": "← back to menu", "value": "back"})

    run_id = inquirer.select(
        message=f"runs — {len(runs)} found (newest first)",
        choices=choices,
    ).execute()
    if run_id == "back":
        return

    log = next(r for r in runs if r["id"] == run_id)

    hitl_suspensions = find_run_suspensions(_SUSPENSIONS, run_id)
    hitl_choice = (
        {"name": f"hitl               {len(hitl_suspensions)} suspension(s) — gate decisions and inputs",
         "value": "hitl"}
        if hitl_suspensions else
        {"name": "hitl               (none for this run)", "value": "hitl_none"}
    )
    view = inquirer.select(
        message="view",
        choices=[
            {"name": "decision log       Governance record, tool calls, sources", "value": "decision"},
            {"name": "model invocations  Turn-by-turn model I/O and blocks",      "value": "invocations"},
            hitl_choice,
            {"name": "← back",                                                    "value": "back"},
        ],
    ).execute()
    if view in ("back", "hitl_none"):
        return

    if view == "decision":
        print_decision_log(log)
    elif view == "hitl":
        print_hitl(hitl_suspensions)
    else:
        run_dir = _ARTIFACTS / "runs"
        jsonl = next(
            (p for p in run_dir.rglob(f"{run_id}/model_invocations.jsonl")), None)
        if jsonl is None:
            console.print("[dark_orange]model_invocations.jsonl not found for this run.[/]")
            return
        invocations = [
            json.loads(line)
            for line in jsonl.read_text().splitlines() if line.strip()
        ]
        print_model_invocations(invocations)


# ── menu ─────────────────────────────────────────────────────────────────────

Action = namedtuple("Action", "name desc fn")

ACTIONS = [
    Action("status",      "Show Docker service health",            status_action),
    Action("up",          "Start the Docker stack",                up_action),
    Action("down",        "Stop the stack",                        down_action),
    Action("seed",        "Seed MinIO demo documents",             seed_action),
    Action("run",         "Execute a demo scenario",               run_action),
    Action("runs",        "Browse decision logs and model turns",  runs_action),
    Action("logs",        "Tail a service log",                    logs_action),
    Action("suspensions", "Browse and action HITL suspensions",    suspensions_action),
]
_BY_NAME = {a.name: a for a in ACTIONS}


def menu() -> None:
    choices = [{"name": f"{a.name:14} {a.desc}", "value": a.name} for a in ACTIONS]
    choices.append({"name": "quit", "value": "quit"})
    while True:
        pick = inquirer.select(message="agent-harness · demo", choices=choices).execute()
        if pick == "quit":
            return
        _BY_NAME[pick].fn()


if __name__ == "__main__":
    menu()
