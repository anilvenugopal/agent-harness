# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine factory — wire the parts into a runnable ExecutionEngine.

Two entry points:

  build_engine(...)        : full control — pass your own providers, connectors,
                             sink, continuation store, tracer.
  build_default_engine()   : opinionated default for the demo — registers all
                             available real providers (only those whose API key
                             is present in the env) PLUS the mock provider, a
                             FileSink, a FileContinuationStore, and connectors
                             built from env (S3/Postgres) when configured.

The factory is deliberately the ONLY place that knows about concrete providers
and connectors. Swapping the decision sink (file → Postgres) or the dispatch
source (direct → worker) is a change here, not in the engine.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from harness.connectors.base import ConnectorRegistry
from harness.connectors.binder import Binder
from harness.core.engine import ExecutionEngine
from harness.core.package import Package
from harness.core.trace import Tracer
from harness.decisions.assembler import DecisionSink, FileSink, PostgresSink
from harness.hitl.continuation import ContinuationStore, FileContinuationStore
from harness.mcp.client import MCPClient
from harness.providers.base import ModelChain, ModelProvider
from harness.providers.mock_provider import MockProvider, ScriptedTurn
from harness.tools.gateway import ToolGateway
from harness.tools.python_tools import PythonToolRegistry


def load_packages(*paths: str) -> dict[str, Package]:
    pkgs: dict[str, Package] = {}
    for p in paths:
        path = Path(p)
        files = path.rglob("*.yaml") if path.is_dir() else [path]
        for f in files:
            pkg = Package.load(f)
            pkgs[pkg.name] = pkg
    return pkgs


def build_providers(mock_turns: Optional[list[ScriptedTurn]] = None,
                    mock_replay: Optional[list[dict]] = None) -> dict[str, ModelProvider]:
    """Register every provider whose dependency + key is available, plus mock.

    Real adapters are imported lazily and only registered when their API key is
    set, so the offline path needs none of the vendor SDKs installed.
    """
    providers: dict[str, ModelProvider] = {
        "mock": MockProvider(turns=mock_turns, replay=mock_replay),
    }
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from harness.providers.anthropic_provider import AnthropicProvider
            providers["anthropic"] = AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"])
        except Exception as e:  # pragma: no cover
            print(f"[factory] anthropic provider unavailable: {e}")
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from harness.providers.openai_provider import OpenAIProvider
            providers["openai"] = OpenAIProvider(api_key=os.environ["OPENAI_API_KEY"])
        except Exception as e:  # pragma: no cover
            print(f"[factory] openai provider unavailable: {e}")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        try:
            from harness.providers.gemini_provider import GeminiProvider
            providers["gemini"] = GeminiProvider(
                api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"])
        except Exception as e:  # pragma: no cover
            print(f"[factory] gemini provider unavailable: {e}")
    return providers


def build_connectors() -> ConnectorRegistry:
    """Build connectors from env. Each is optional; absent config = not registered."""
    reg = ConnectorRegistry()
    if os.environ.get("PG_MAIN_DSN"):
        from harness.connectors.postgres import PostgresConnector
        reg.register(PostgresConnector("pg_main", os.environ["PG_MAIN_DSN"]))
    if os.environ.get("S3_ENDPOINT_URL"):
        from harness.connectors.s3 import S3Connector
        reg.register(S3Connector(
            "s3_main",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            access_key=os.environ.get("S3_ACCESS_KEY"),
            secret_key=os.environ.get("S3_SECRET_KEY"),
        ))
    return reg


def build_engine(
    *,
    packages: dict[str, Package],
    providers: dict[str, ModelProvider],
    connectors: Optional[ConnectorRegistry] = None,
    python_tools: Optional[PythonToolRegistry] = None,
    mcp_client: Optional[MCPClient] = None,
    sink: Optional[DecisionSink] = None,
    continuation_store: Optional[ContinuationStore] = None,
    tracer: Optional[Tracer] = None,
    application: str = "harness",
    global_fallback_enabled: bool = True,
) -> ExecutionEngine:
    chain = ModelChain(providers)
    gateway = ToolGateway(
        python_tools=python_tools or PythonToolRegistry(),
        mcp_client=mcp_client,
    )
    binder = Binder(connectors or ConnectorRegistry())
    return ExecutionEngine(
        chain=chain, tool_gateway=gateway, binder=binder, packages=packages,
        sink=sink, continuation_store=continuation_store, tracer=tracer,
        application=application, global_fallback_enabled=global_fallback_enabled,
    )


def build_default_engine(
    *,
    package_paths: tuple[str, ...] = ("packages",),
    artifacts_root: str = "./_artifacts",
    trace: bool = True,
    step: bool = False,
    mock_turns: Optional[list[ScriptedTurn]] = None,
    mock_replay: Optional[list[dict]] = None,
    python_tools: Optional[PythonToolRegistry] = None,
    mcp_servers: Optional[dict[str, dict]] = None,
) -> ExecutionEngine:
    packages = load_packages(*package_paths)
    providers = build_providers(mock_turns=mock_turns, mock_replay=mock_replay)
    connectors = build_connectors()
    sink: DecisionSink = (PostgresSink(os.environ["DECISION_PG_DSN"])
                          if os.environ.get("DECISION_SINK") == "postgres" and os.environ.get("DECISION_PG_DSN")
                          else FileSink(artifacts_root))
    fallback_raw = os.environ.get("HARNESS_FALLBACK_ENABLED", "true").strip().lower()
    global_fallback_enabled = fallback_raw not in ("false", "0", "no")
    return build_engine(
        packages=packages, providers=providers, connectors=connectors,
        python_tools=python_tools or PythonToolRegistry(),
        mcp_client=MCPClient(mcp_servers or {}),
        sink=sink,
        continuation_store=FileContinuationStore(f"{artifacts_root}/suspensions"),
        tracer=Tracer(enabled=trace, step=step),
        global_fallback_enabled=global_fallback_enabled,
    )


__all__ = [
    "load_packages", "build_providers", "build_connectors",
    "build_engine", "build_default_engine",
]
