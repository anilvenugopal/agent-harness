"""In-process Python tools (ADR-0016 Category B-ish / built-in callables).

A tiny registry mapping tool name → callable. The worker/app registers these
at startup; the gateway dispatches to them when a tool's transport is
`python_inprocess`. Both sync and async callables are supported.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


class PythonToolRegistry:
    def __init__(self):
        self._impls: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._impls[name] = fn

    def has(self, name: str) -> bool:
        return name in self._impls

    async def call(self, name: str, tool_input: dict) -> Any:
        fn = self._impls[name]
        if inspect.iscoroutinefunction(fn):
            return await fn(**tool_input)
        result = fn(**tool_input)
        # allow a sync fn that returns an awaitable
        if inspect.isawaitable(result):
            return await result
        return result


__all__ = ["PythonToolRegistry"]
