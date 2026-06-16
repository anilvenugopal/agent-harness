"""Decision-log, model-invocation, and HITL renderers for the demo CLI."""
from __future__ import annotations

import json
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

_TRUNC = 120   # max chars before we cut a JSON value in tables


def _j(v, trunc: int = _TRUNC) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s[:trunc] + "…" if len(s) > trunc else s


# ── run discovery ─────────────────────────────────────────────────────────────

def list_runs(artifacts_root: Path) -> list[dict]:
    """Return decision_log dicts for all runs, newest first."""
    runs = []
    runs_dir = artifacts_root / "runs"
    if not runs_dir.exists():
        return runs
    for log_path in sorted(runs_dir.rglob("decision_log.json"), reverse=True):
        try:
            runs.append(json.loads(log_path.read_text()))
        except Exception:
            pass
    return runs


# ── decision log view ─────────────────────────────────────────────────────────

def print_decision_log(log: dict) -> None:
    status  = log.get("status", "?")
    color   = "green" if status == "complete" else "red"
    depth   = log.get("decision_depth", 0)
    depth_s = f"  depth={depth}" if depth else ""

    header = (
        f"[bold]{log['entity_name']}[/]  v{log['entity_version']}  "
        f"[{color}]{status}[/]{depth_s}\n"
        f"[dim]id:[/]       {log['id']}\n"
        f"[dim]created:[/]  {log['created_at'][:19]}\n"
        f"[dim]duration:[/] {log['duration_ms']}ms   "
        f"[dim]tokens:[/] in={log['input_tokens']} out={log['output_tokens']}"
        + (f"  cache_read={log['cache_read_tokens']}" if log.get('cache_read_tokens') else "")
        + f"\n[dim]models:[/]  {', '.join(log.get('models_used') or ['—'])}"
        + (f"\n[dim]channel:[/] {log['channel']}" if log.get('channel') else "")
        + ("\n[yellow]hitl:[/]     required" if log.get("hitl_required") else "")
    )
    if log.get("parent_decision_id"):
        header += f"\n[dim]parent:[/]  {log['parent_decision_id']}"

    console.print(Panel(header, box=box.ROUNDED, expand=False))

    # ── input / output ──
    console.print("\n[bold]Input[/]")
    console.print_json(json.dumps(log.get("input_json", {})))

    if log.get("output_json"):
        console.print("\n[bold]Output[/]")
        console.print_json(json.dumps(log["output_json"]))

    if log.get("reasoning_text"):
        console.print(f"\n[bold]Reasoning[/]\n[dim]{log['reasoning_text'][:600]}[/]"
                      + ("…" if len(log["reasoning_text"]) > 600 else ""))

    if log.get("error_message"):
        console.print(f"\n[red bold]Error[/]\n{log['error_message']}")

    # ── source resolutions ──
    resolutions = log.get("source_resolutions") or []
    if resolutions:
        console.print("\n[bold]Source Resolutions[/]")
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("bind_to")
        t.add_column("connector")
        t.add_column("method")
        t.add_column("result")
        for r in resolutions:
            ok = r.get("error") is None and not r.get("mocked", False)
            mocked = r.get("mocked", False)
            tag = "[dim](mock)[/]" if mocked else ("[green]ok[/]" if ok else "[red]error[/]")
            preview = r.get("error") or r.get("value_preview") or ""
            t.add_row(r.get("bind_to", ""), r.get("connector", ""),
                      r.get("method", ""), f"{tag}  {_j(preview)}")
        console.print(t)

    # ── tool calls ──
    tool_calls = log.get("tool_calls_made") or []
    if tool_calls:
        console.print("\n[bold]Tool Calls[/]")
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("#", justify="right", style="dim")
        t.add_column("tool")
        t.add_column("transport")
        t.add_column("result")
        for tc in tool_calls:
            idx   = str(tc.get("call_order", ""))
            name  = tc.get("tool_name", "?")
            trans = tc.get("transport", "")
            err   = tc.get("error", False)
            out   = tc.get("output_data", "")
            tag   = "[red]error[/]" if err else "[green]ok[/]"
            t.add_row(idx, name, trans, f"{tag}  {_j(out)}")
        console.print(t)

    # ── target writes ──
    writes = log.get("target_writes") or []
    if writes:
        console.print("\n[bold]Target Writes[/]")
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("connector")
        t.add_column("container")
        t.add_column("result")
        for w in writes:
            err = w.get("error", False)
            tag = "[red]error[/]" if err else "[green]ok[/]"
            t.add_row(w.get("connector", ""), w.get("container", ""),
                      f"{tag}  {_j(w.get('handle') or w.get('error_message') or '')}")
        console.print(t)

    console.print()


# ── model invocations view ────────────────────────────────────────────────────

def print_model_invocations(invocations: list[dict]) -> None:
    if not invocations:
        console.print("[dim]No model invocations recorded.[/]")
        return

    for inv in invocations:
        turn     = inv.get("turn", "?")
        provider = inv.get("provider", "?")
        model    = inv.get("model", "?")
        stop     = inv.get("stop_reason", "?")
        usage    = inv.get("usage", {})
        in_tok   = usage.get("input_tokens", 0)
        out_tok  = usage.get("output_tokens", 0)

        console.print(
            f"\n[bold dim]turn {turn}[/]  "
            f"[cyan]{provider}[/]  [cyan]{model}[/]  "
            f"stop=[yellow]{stop}[/]  "
            f"[dim]in={in_tok} out={out_tok}[/]"
        )

        for block in inv.get("blocks", []):
            btype = block.get("type", "?")
            if btype == "text":
                text = block.get("text", "")
                preview = text[:300] + ("…" if len(text) > 300 else "")
                console.print(f"  [dim]text[/]  {preview}")

            elif btype == "tool_use":
                name  = block.get("name", "?")
                inp   = _j(block.get("input", {}), trunc=200)
                console.print(f"  [green]tool_use[/]  [bold]{name}[/]({inp})")

            elif btype == "tool_result":
                tid     = block.get("tool_use_id", "")[:8]
                content = _j(block.get("content", ""), trunc=200)
                is_err  = block.get("is_error", False)
                tag     = "[red]error[/]" if is_err else "[dim]result[/]"
                console.print(f"  {tag}  [{tid}…]  {content}")

            else:
                console.print(f"  [dim]{btype}[/]  {_j(block, trunc=120)}")

    console.print()


# ── HITL view ────────────────────────────────────────────────────────────────

def find_run_suspensions(suspensions_root: Path, decision_id: str) -> list[dict]:
    """Return all suspension records for a given decision log id, sorted by created_at."""
    results = []
    for p in suspensions_root.glob("*.json"):
        try:
            s = json.loads(p.read_text())
            if str(s.get("decision_id")) == str(decision_id):
                results.append(s)
        except Exception:
            pass
    return sorted(results, key=lambda s: s.get("created_at", ""))


def print_hitl(suspensions: list[dict]) -> None:
    for s in suspensions:
        status   = s.get("status", "?")
        resolved = status == "resolved"
        color    = "green" if resolved else "yellow"

        header = (
            f"[bold]{s['gate_tool']}[/]  [{color}]{status}[/]\n"
            f"[dim]suspension_id:[/] {s['id']}\n"
            f"[dim]suspended:[/]     {s['created_at'][:19]}"
        )
        console.print(Panel(header, box=box.ROUNDED, expand=False))

        inp = s.get("pending_tool_use", {}).get("input", {})
        if inp:
            console.print("[bold]Tool input[/]")
            console.print_json(json.dumps(inp))

        dec = s.get("decision")
        if dec:
            console.print(
                f"\n[bold]Decision[/]  [{color}]{dec['decision']}[/]"
                + (f"  by {dec['decided_by']}" if dec.get("decided_by") else "")
                + (f"  at {dec['decided_at'][:19]}" if dec.get("decided_at") else "")
            )
            if dec.get("note"):
                console.print(f"  note: {dec['note']}")
        else:
            console.print("\n[yellow]Awaiting decision[/]")

        console.print()
