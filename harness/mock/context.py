# SPDX-License-Identifier: AGPL-3.0-or-later
"""MockContext — playback/suppress control for all four gateways at once.

This is the single object that flips any combination of the engine's four
effect gateways into mock or suppress mode. It is the runtime expression of
the "every gateway has a mock seam" spine, and it is what makes audit
reproduction a single call rather than a bespoke feature:

  - model:   `model_replay` feeds the model gateway recorded turns
             (deterministic LLM playback). If set, real providers are bypassed.
  - tools:   `tool_responses` / `mock_all_tools` short-circuit the tool gateway
             with canned outputs (no real tool or MCP call).
  - sources: `source_responses` short-circuit the connector fetch (no real
             S3/Postgres read).
  - targets: `suppress_targets` makes the connector write a no-op that still
             records an audit entry (shadow/challenger mode, ADR-0016 Cat C),
             OR `target_handles` supplies canned write handles for replay.

Precedence inside a gateway: an explicit MockContext entry wins over the
package's per-tool default mock. When NO MockContext is supplied, gateways use
each tool's package-declared `mock_response` default (if any) and otherwise
run live.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class MockContext(BaseModel):
    # ── MODEL ──
    # Recorded model-invocation records (FileSink shape). When present, the
    # ModelChain is replaced by a MockProvider(replay=...) for the whole run.
    model_replay: Optional[list[dict]] = None

    # ── TOOLS / MCP ──
    tool_responses: dict[str, Any] = Field(default_factory=dict)
    mock_all_tools: bool = False

    # ── SOURCES ──
    # keyed by SourceBinding.bind_to
    source_responses: dict[str, Any] = Field(default_factory=dict)

    # ── TARGETS ──
    suppress_targets: bool = False
    # keyed by TargetBinding.from_path → canned write handle (for replay)
    target_handles: dict[str, Any] = Field(default_factory=dict)

    @property
    def active(self) -> bool:
        """True if any mocking/suppression is in effect — sets decision.mock_mode."""
        return bool(
            self.model_replay or self.tool_responses or self.mock_all_tools
            or self.source_responses or self.suppress_targets or self.target_handles
        )

    def tool_response_for(self, name: str) -> Optional[Any]:
        return self.tool_responses.get(name)


__all__ = ["MockContext"]
