"""Input/output binder — works over the connector registry.

The binder is the layer between the package's declarative bindings and the raw
connectors. It owns:

  - SOURCE RESOLUTION: for each SourceBinding, template the ref with the run's
    input, fetch via the named connector, and place the value under
    `bind_to` in the run context. The MOCK SEAM short-circuits this when a
    MockContext supplies `source_responses[bind_to]` (offline / replay).

  - TARGET WRITES: for each TargetBinding, select the value out of the output
    dict (`from_path`), template the container, and write via the connector.
    The SUPPRESS SEAM turns this into an audited no-op when
    MockContext.suppress_targets is set (shadow/challenger read-only mode,
    ADR-0016 Category C "write-target suppressor"), and the REPLAY SEAM returns
    a canned handle from `target_handles`.

Every resolution and write produces an audit dict appended to the decision
record (source_resolutions / target_writes), so the governance record shows
exactly what was read and written — or suppressed.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from harness.connectors.base import (
    ConnectorRegistry, SourceResolutionError, TargetWriteError,
)
from harness.core.package import SourceBinding, TargetBinding
from harness.mock.context import MockContext

logger = logging.getLogger("harness.connectors.binder")

_TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _template(value: Any, scope: dict) -> Any:
    """Replace {{a.b.c}} tokens in a string using dotted lookups into scope."""
    if not isinstance(value, str):
        return value

    def repl(m):
        path = m.group(1)
        cur: Any = scope
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
            if cur is None:
                break
        return str(cur if cur is not None else "")

    return _TEMPLATE.sub(repl, value)


def _select(output: dict, from_path: str) -> Any:
    """Tiny JSONPath-ish selector: '$' = whole dict, '$.a.b' = nested."""
    if from_path in ("$", ""):
        return output
    path = from_path[2:] if from_path.startswith("$.") else from_path
    cur: Any = output
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class Binder:
    def __init__(self, connectors: ConnectorRegistry):
        self.connectors = connectors

    async def resolve_sources(
        self,
        sources: list[SourceBinding],
        run_input: dict,
        mock: Optional[MockContext],
    ) -> tuple[dict, list[dict]]:
        """Return (context_additions, audit_records)."""
        context: dict = {}
        audit: list[dict] = []
        scope = {"input": run_input}
        for src in sources:
            # MOCK SEAM
            if mock is not None and src.bind_to in mock.source_responses:
                value = mock.source_responses[src.bind_to]
                context[src.bind_to] = value
                audit.append(_src_audit(src, value, mocked=True))
                continue
            try:
                ref = _template(src.ref, scope)
                connector = self.connectors.get(src.connector)
                value = await connector.fetch(src.method, ref)
                context[src.bind_to] = value
                audit.append(_src_audit(src, value, mocked=False))
            except Exception as e:
                logger.warning("source resolution failed: %s (%s)", src.bind_to, e)
                audit.append(_src_audit(src, None, mocked=False, error=str(e)))
                if src.required:
                    raise SourceResolutionError(
                        f"required source {src.bind_to!r} via {src.connector!r} failed: {e}"
                    ) from e
        return context, audit

    async def write_targets(
        self,
        targets: list[TargetBinding],
        output: dict,
        run_input: dict,
        run_id: str,
        mock: Optional[MockContext],
    ) -> list[dict]:
        audit: list[dict] = []
        scope = {"input": run_input, "output": output, "run_id": run_id}
        for tgt in targets:
            value = _select(output, tgt.from_path)
            container = _template(tgt.container, scope) if tgt.container else None

            # SUPPRESS SEAM (shadow/challenger / dry-run)
            if mock is not None and mock.suppress_targets:
                audit.append(_tgt_audit(tgt, container, handle=None, suppressed=True))
                continue
            # REPLAY SEAM
            if mock is not None and tgt.from_path in mock.target_handles:
                audit.append(_tgt_audit(tgt, container, handle=mock.target_handles[tgt.from_path],
                                        suppressed=False, replayed=True))
                continue
            try:
                connector = self.connectors.get(tgt.connector)
                handle = await connector.write(tgt.method, container, value)
                audit.append(_tgt_audit(tgt, container, handle=handle, suppressed=False))
            except Exception as e:
                logger.warning("target write failed: %s (%s)", tgt.from_path, e)
                audit.append(_tgt_audit(tgt, container, handle=None, suppressed=False, error=str(e)))
                if tgt.required:
                    raise TargetWriteError(
                        f"required target {tgt.from_path!r} via {tgt.connector!r} failed: {e}"
                    ) from e
        return audit


def _src_audit(src: SourceBinding, value, *, mocked, error=None) -> dict:
    return {
        "bind_to": src.bind_to, "connector": src.connector, "method": src.method,
        "mocked": mocked, "error": error,
        "value_preview": _preview(value),
    }


def _tgt_audit(tgt: TargetBinding, container, *, handle, suppressed, replayed=False, error=None) -> dict:
    return {
        "from_path": tgt.from_path, "connector": tgt.connector, "method": tgt.method,
        "container": container, "suppressed": suppressed, "replayed": replayed,
        "handle": handle, "error": error,
    }


def _preview(value) -> str:
    s = str(value)
    return s[:200] + ("..." if len(s) > 200 else "")


__all__ = ["Binder"]
