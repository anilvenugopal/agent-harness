"""Model providers — the seam between the loop and the LLM vendors.

A `ModelProvider` does exactly one thing: take a neutral request (system
prompt + neutral Messages + neutral ToolDefs + a ModelRef) and return a
neutral `ModelResponse`. It owns ALL vendor-specific translation. The loop
never sees a vendor SDK.

`ModelChain` wraps a priority-ordered list of (provider, ModelRef) links and
implements ADR-0019's reference-chain fallback: try priority 0 with retries;
on exhausted retries (or a hard non-retryable error), fall to priority 1; and
so on. This is also the multi-provider fallback we discussed — Claude primary,
Gemini fallback, OpenAI last resort, all behind one `.complete()` call.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Protocol, runtime_checkable

from harness.core.ir import Message, ModelResponse, ToolDef
from harness.core.package import ModelRef

logger = logging.getLogger("harness.providers")


class ProviderError(Exception):
    """Base for provider failures."""


class RetryableProviderError(ProviderError):
    """A transient failure (429/5xx/network). The chain retries within a link,
    then falls to the next link if retries are exhausted.
    """


class FatalProviderError(ProviderError):
    """A non-retryable failure (auth, bad request). Skip straight to the next
    chain link — retrying the same call won't help.
    """


@runtime_checkable
class ModelProvider(Protocol):
    """Implemented by anthropic/openai/gemini/mock adapters.

    `complete` MUST translate transient vendor errors to RetryableProviderError
    and permanent ones to FatalProviderError, so the chain can make the right
    fallback decision without knowing vendor specifics.
    """
    name: str

    async def complete(
        self,
        *,
        ref: ModelRef,
        system: str,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        force_tool: Optional[str] = None,
    ) -> ModelResponse: ...


# ──────────────────────────────────────────────────────────────────────
# THE CHAIN — retry-with-jitter inside a link, fall-through across links
# ──────────────────────────────────────────────────────────────────────

class ModelChain:
    """Resolves a package's model chain to a concrete response, with retries
    and cross-provider fallback.

    Parameters mirror production knobs:
      max_retries  : attempts per link before falling through
      base_delay   : exponential-backoff base (seconds)
      max_delay    : cap on a single backoff sleep

    Backoff is exponential WITH JITTER — without jitter, N workers that all hit
    a 429 at the same instant back off in lockstep and re-collide (the
    thundering-herd bug in the v1 engine's retry path).
    """

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ):
        self.providers = providers
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    async def complete(
        self,
        *,
        chain: list[ModelRef],
        system: str,
        messages: list[Message],
        tools: Optional[list[ToolDef]] = None,
        force_tool: Optional[str] = None,
        on_event=None,
    ) -> ModelResponse:
        ordered = sorted(chain, key=lambda m: m.priority)
        last_error: Optional[Exception] = None

        for link in ordered:
            provider = self.providers.get(link.provider)
            if provider is None:
                logger.warning("no provider registered for %r; skipping link", link.provider)
                last_error = FatalProviderError(f"provider {link.provider!r} not registered")
                continue

            for attempt in range(self.max_retries + 1):
                try:
                    if on_event:
                        on_event("model_attempt", provider=link.provider, model=link.model,
                                 priority=link.priority, attempt=attempt)
                    return await provider.complete(
                        ref=link, system=system, messages=messages,
                        tools=tools, force_tool=force_tool,
                    )
                except RetryableProviderError as e:
                    last_error = e
                    if attempt < self.max_retries:
                        delay = self._backoff(attempt)
                        logger.warning(
                            "transient error on %s/%s (attempt %d/%d), retrying in %.1fs: %s",
                            link.provider, link.model, attempt + 1, self.max_retries + 1, delay, e,
                        )
                        if on_event:
                            on_event("model_retry", provider=link.provider, delay=delay, error=str(e))
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("retries exhausted on %s/%s; falling through", link.provider, link.model)
                    if on_event:
                        on_event("model_fallthrough", provider=link.provider, error=str(e))
                    break  # fall to next chain link
                except FatalProviderError as e:
                    last_error = e
                    logger.warning("fatal error on %s/%s; falling through: %s", link.provider, link.model, e)
                    if on_event:
                        on_event("model_fallthrough", provider=link.provider, error=str(e))
                    break  # don't retry a fatal error; try the next link

        raise ProviderError(
            f"model chain exhausted; all {len(ordered)} link(s) failed. Last error: {last_error}"
        ) from last_error

    def _backoff(self, attempt: int) -> float:
        # Full-jitter: sleep ~ Uniform(0, min(max_delay, base * 2**attempt)).
        ceiling = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(0, ceiling)


__all__ = [
    "ModelProvider", "ModelChain",
    "ProviderError", "RetryableProviderError", "FatalProviderError",
]
