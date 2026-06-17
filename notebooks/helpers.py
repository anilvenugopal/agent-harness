# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendering and engine utilities for the walkthrough notebook.

Kept separate so notebook cells focus on the demo flow, not boilerplate.
Imported after cell-01 sets up sys.path.
"""
from __future__ import annotations

import inspect
import json

from rich.console import Console

from harness.core.package import Package
from harness.core.result  import RunStatus

console = Console(force_jupyter=True)


# ── result display ────────────────────────────────────────────────────────────

def show_result(result) -> None:
    """Print a run result summary to the Rich console."""
    color = {"complete": "green", "suspended": "dark_orange"}.get(result.status.value, "red")
    console.rule(f"[{color} bold]{result.entity_name}  —  {result.status.value}[/]")
    console.print(f"  run_id   : [dim]{result.run_id}[/]")
    tok = f"in={result.usage.input_tokens}  out={result.usage.output_tokens}"
    if result.usage.cache_read_tokens:
        tok += f"  cache_read={result.usage.cache_read_tokens}"
    console.print(f"  tokens   : {tok}")
    console.print(f"  duration : {result.duration_ms}ms")
    if result.output:
        console.print("\n[bold]Output[/]")
        console.print_json(json.dumps(result.output))
    if result.error_message:
        console.print(f"\n[red bold]Error[/]  {result.error_message}")


# ── engine teardown ───────────────────────────────────────────────────────────

async def close_engine(engine) -> None:
    """Drain the MCP session and provider HTTP connection pools."""
    if engine.tools.mcp_client:
        await engine.tools.mcp_client.close_all()
    for provider in engine.chain.providers.values():
        client = getattr(provider, "_client", None)
        if client:
            fn = getattr(client, "close", None)
            if fn and inspect.iscoroutinefunction(fn):
                try:
                    await fn()
                except Exception:
                    pass


# ── HTML primitives ───────────────────────────────────────────────────────────

def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:white;padding:2px 9px;'
        f'border-radius:4px;font-size:0.78em;font-weight:600;'
        f'letter-spacing:0.02em">{text}</span>'
    )


def _table(headers: list, rows: list, stripe: bool = True) -> str:
    ths = "".join(
        f'<th style="padding:5px 10px;text-align:left;white-space:nowrap">{h}</th>'
        for h in headers
    )
    trs = ""
    for i, row in enumerate(rows):
        bg  = "#f8fafc" if (stripe and i % 2 == 0) else "white"
        tds = "".join(
            f'<td style="padding:4px 10px;vertical-align:top">{v}</td>' for v in row
        )
        trs += f'<tr style="background:{bg}">{tds}</tr>'
    return (
        f'<table style="border-collapse:collapse;width:100%;font-size:0.88em;font-family:monospace">'
        f'<thead><tr style="background:#1e3a5f;color:white">{ths}</tr></thead>'
        f'<tbody>{trs}</tbody></table>'
    )


_TRANSPORT_COLORS: dict[str, str] = {
    "python_inprocess": "#81be97",
    "mcp_http":         "#91A5CF",
    "mcp_stdio":        "#9781bd",
    "verity_builtin":   "#805C48",
}
_KIND_COLORS: dict[str, str]     = {"agent": "#91A5CF", "task": "#a27d61"}
_PROVIDER_COLORS: dict[str, str] = {
    "anthropic": "#bd7979",
    "openai":    "#81be97",
    "gemini":    "#91A5CF",
    "mock":      "#6b7280",
}


# ── package card renderer ─────────────────────────────────────────────────────

def render_package_card(pkg: Package) -> str:
    """Return a self-contained HTML card for a Package."""
    hitl_gates = set(pkg.hitl.require_approval_for) if pkg.hitl.enabled else set()

    badges = [
        _badge(pkg.kind.value, _KIND_COLORS.get(pkg.kind.value, "#6b7280")),
        _badge(pkg.version, "#6b7280"),
    ]
    if hitl_gates:
        badges.append(_badge("⚠ HITL GATE", "#bd7979"))
    if pkg.delegations:
        badges.append(_badge("delegates", "#9781bd"))

    header = (
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
        f'<span style="font-size:1.15em;font-weight:700;font-family:monospace">{pkg.name}</span>'
        f'{"&nbsp;".join(badges)}</div>'
        f'<p style="color:#4b5563;margin:0 0 14px;font-size:0.92em">{pkg.description}</p>'
    )

    prompt_html = (
        f'<details style="margin-bottom:14px">'
        f'<summary style="cursor:pointer;font-weight:600;color:#374151;font-size:0.9em">System prompt</summary>'
        f'<pre style="background:#1e293b;color:#e2e8f0;padding:12px;border-radius:6px;'
        f'white-space:pre-wrap;font-size:0.82em;margin-top:8px;'
        f'max-height:220px;overflow-y:auto">{pkg.system_prompt.strip()}</pre>'
        f'</details>'
    )

    chain_rows = [
        [str(i + 1),
         _badge(m.provider, _PROVIDER_COLORS.get(m.provider, "#6b7280")),
         f'<code>{m.model}</code>',
         f'{m.max_tokens:,}']
        for i, m in enumerate(pkg.inference.ordered())
    ]
    budget = f'max_turns: <b>{pkg.inference.max_turns}</b>'
    if pkg.inference.max_usd:
        budget += f' &nbsp;·&nbsp; budget cap: <b>${pkg.inference.max_usd}/run</b>'
    inference_html = (
        f'<div style="margin-bottom:14px">'
        f'<div style="font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;'
        f'padding-bottom:4px;margin-bottom:8px">Inference chain</div>'
        f'<p style="color:#6b7280;font-size:0.88em;margin:0 0 8px">{budget}</p>'
        f'{_table(["#", "Provider", "Model", "Max tokens"], chain_rows)}'
        f'</div>'
    )

    sources_html = ""
    if pkg.sources:
        src_rows = [
            [f'<code>{s.connector}</code>', s.method, f'<code>{s.bind_to}</code>',
             f'<code style="color:#6b7280;font-size:0.85em">{str(s.ref)[:72]}</code>',
             (_badge("as_block", "#9781bd") + "&nbsp;" if s.as_block else "") +
             (_badge("required", "#bd7979") if s.required else _badge("optional", "#6b7280"))]
            for s in pkg.sources
        ]
        sources_html = (
            f'<div style="margin-bottom:14px">'
            f'<div style="font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;'
            f'padding-bottom:4px;margin-bottom:8px">Sources '
            f'<span style="font-weight:400;color:#6b7280;font-size:0.88em">'
            f'(resolved before the run; injected into context)</span></div>'
            f'{_table(["Connector", "Method", "Bind to", "Ref / query", "Flags"], src_rows)}'
            f'</div>'
        )

    tools_html = ""
    if pkg.tools:
        tool_rows = []
        for t in pkg.tools:
            hitl_marker = "&nbsp;" + _badge("HITL GATE", "#bd7979") if t.name in hitl_gates else ""
            mcp_note = (f' <span style="color:#6b7280;font-size:0.85em">→ {t.mcp_server}</span>'
                        if t.mcp_server else "")
            desc = t.description[:110] + ("…" if len(t.description) > 110 else "")
            tool_rows.append([
                f'<code><b>{t.name}</b></code>{hitl_marker}',
                _badge(t.transport, _TRANSPORT_COLORS.get(t.transport, "#6b7280")) + mcp_note,
                f'<span style="color:#374151;font-size:0.88em">{desc}</span>',
            ])
        tools_html = (
            f'<div style="margin-bottom:14px">'
            f'<div style="font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;'
            f'padding-bottom:4px;margin-bottom:8px">Authorized tools '
            f'<span style="font-weight:400;color:#6b7280;font-size:0.88em">'
            f'(engine enforces this list before every dispatch)</span></div>'
            f'{_table(["Tool", "Transport", "Description"], tool_rows)}'
            f'</div>'
        )

    deleg_html = ""
    if pkg.delegations:
        pills = " ".join(_badge(d.child_agent, "#9781bd") for d in pkg.delegations)
        deleg_html = (
            f'<div style="margin-bottom:14px">'
            f'<div style="font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;'
            f'padding-bottom:4px;margin-bottom:8px">Delegations '
            f'<span style="font-weight:400;color:#6b7280;font-size:0.88em">'
            f'(each spawns a fully governed nested run with its own decision record)</span></div>'
            f'<p style="font-size:0.9em">May spawn: {pills}</p>'
            f'</div>'
        )

    targets_html = ""
    if pkg.targets:
        tgt_rows = [
            [f'<code>{t.connector}</code>', t.method, f'<code>{t.from_path}</code>',
             f'<code style="color:#6b7280">{t.container or ""}</code>',
             _badge("required", "#bd7979") if t.required else _badge("optional", "#6b7280")]
            for t in pkg.targets
        ]
        targets_html = (
            f'<div style="margin-bottom:14px">'
            f'<div style="font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;'
            f'padding-bottom:4px;margin-bottom:8px">Targets '
            f'<span style="font-weight:400;color:#6b7280;font-size:0.88em">'
            f'(written after the run produces its output)</span></div>'
            f'{_table(["Connector", "Method", "From path", "Container", ""], tgt_rows)}'
            f'</div>'
        )

    schema_html = ""
    if pkg.output_schema:
        schema_rows = [
            [f'<code><b>{k}</b></code>', v.get("type", "?"),
             ", ".join(f'<code>{e}</code>' for e in v.get("enum", [])) or "—"]
            for k, v in pkg.output_schema.items()
        ]
        schema_html = (
            f'<div>'
            f'<div style="font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;'
            f'padding-bottom:4px;margin-bottom:8px">Output schema '
            f'<span style="font-weight:400;color:#6b7280;font-size:0.88em">'
            f'(enforced via submit_output / structured_output)</span></div>'
            f'{_table(["Field", "Type", "Enum values"], schema_rows)}'
            f'</div>'
        )

    return (
        f'<div style="border:1px solid #d1d5db;border-radius:10px;padding:20px;'
        f'margin:0 0 24px;background:white;max-width:960px">'
        f'{header}{prompt_html}{inference_html}'
        f'{sources_html}{tools_html}{deleg_html}{targets_html}{schema_html}'
        f'</div>'
    )


# public alias so notebook cells don't reach into private names
html_table = _table
