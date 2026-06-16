# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quota enforcer (ADR-0016 Category C).

Checked BEFORE every model call. Enforces the per-run ceilings declared on the
package's InferenceConfig: max_turns, max_total_tokens, max_usd. In Verity this
reads a coordinator-local SQLite cache; here it's an in-process budget tracked
per run, which is the same logic minus the distributed cache.

A simple price table converts tokens → USD for the max_usd ceiling. Prices are
illustrative; the real engine would load them from the model registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.core.ir import Usage


class QuotaExceeded(Exception):
    """Raised when a run would exceed its budget. The loop turns this into a
    failed terminal status (not a crash)."""


# illustrative $/1M tokens (input, output)
_PRICES = {
    "claude": (3.0, 15.0),
    "gpt": (2.5, 10.0),
    "gemini": (1.25, 5.0),
    "mock": (0.0, 0.0),
}


def _price_for(model: str) -> tuple[float, float]:
    m = model.lower()
    for key, price in _PRICES.items():
        if key in m:
            return price
    return (3.0, 15.0)  # conservative default


@dataclass
class QuotaEnforcer:
    max_turns: int = 10
    max_total_tokens: int | None = None
    max_usd: float | None = None
    _turns: int = field(default=0, init=False)
    _usage: Usage = field(default_factory=Usage, init=False)
    _usd: float = field(default=0.0, init=False)

    def check_turn(self) -> None:
        """Call before each model turn. Raises if the next turn would exceed
        the turn budget or an already-accrued ceiling."""
        if self._turns >= self.max_turns:
            raise QuotaExceeded(f"max_turns={self.max_turns} reached")
        if self.max_total_tokens is not None:
            total = self._usage.input_tokens + self._usage.output_tokens
            if total >= self.max_total_tokens:
                raise QuotaExceeded(f"max_total_tokens={self.max_total_tokens} reached (used {total})")
        if self.max_usd is not None and self._usd >= self.max_usd:
            raise QuotaExceeded(f"max_usd=${self.max_usd:.2f} reached (spent ${self._usd:.4f})")
        self._turns += 1

    def record(self, usage: Usage, model: str) -> None:
        """Call after each model turn to accrue cost."""
        self._usage = self._usage + usage
        pin, pout = _price_for(model)
        self._usd += (usage.input_tokens / 1_000_000) * pin
        self._usd += (usage.output_tokens / 1_000_000) * pout

    @property
    def spent_usd(self) -> float:
        return self._usd

    @property
    def turns(self) -> int:
        return self._turns


__all__ = ["QuotaEnforcer", "QuotaExceeded"]
