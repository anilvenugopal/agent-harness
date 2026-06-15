"""Connector framework — base contract.

A Connector talks to one external system (Postgres, S3, a REST API). It
exposes two verbs:

  fetch(method, ref)            → resolve an input (a source)
  write(method, container, payload) → persist an output (a target), return a handle

This mirrors Verity's ConnectorProvider Protocol and ADR-0016's "Category B
connector framework baked into the harness image." The engine never imports a
connector's underlying driver directly; connectors own that (and import it
lazily, so the offline path needs no boto3/psycopg installed).

Credentials follow ADR-0010 Model B: the value lives in the environment /
secrets manager and is read by the connector at construction; the engine only
knows the connector's NAME, never its secret.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ConnectorError(Exception):
    """Base for connector failures."""


class ConnectorMethodError(ConnectorError):
    """Provider doesn't implement the requested fetch/write method."""


class SourceResolutionError(ConnectorError):
    """A required source could not be resolved."""


class TargetWriteError(ConnectorError):
    """A required target write failed."""


@runtime_checkable
class Connector(Protocol):
    name: str
    async def fetch(self, method: str, ref: Any) -> Any: ...
    async def write(self, method: str, container: str | None, payload: Any) -> Any: ...
    async def close(self) -> None: ...


class ConnectorRegistry:
    """Name → Connector. Populated at worker/app startup from package +
    environment. Reads are concurrent-safe; registration is startup-only.
    """
    def __init__(self):
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        self._connectors[connector.name] = connector

    def get(self, name: str) -> Connector:
        try:
            return self._connectors[name]
        except KeyError as e:
            raise ConnectorError(
                f"No connector registered for {name!r}. "
                f"Registered: {sorted(self._connectors)}"
            ) from e

    def has(self, name: str) -> bool:
        return name in self._connectors

    async def close_all(self) -> None:
        for c in self._connectors.values():
            try:
                await c.close()
            except Exception:
                pass


__all__ = [
    "Connector", "ConnectorRegistry", "ConnectorError",
    "ConnectorMethodError", "SourceResolutionError", "TargetWriteError",
]
