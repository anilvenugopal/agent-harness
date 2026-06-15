"""Postgres connector — Category B SQL access.

Methods:
  fetch("query", sql)            → list[dict] rows
  fetch("query_one", sql)        → first row dict or None
  write("execute", _, sql)       → {"rowcount": n}

psycopg is imported lazily inside the methods so the offline/mock path doesn't
require the driver. A real connection pool would replace the connect-per-call
here; kept simple for the demo. SQL is expected to be fully formed by the
binder (which does the {{input.x}} templating before handing it over).

NOTE: in production you'd parameterise queries, not string-template them. The
binder templating here is for demo ergonomics; the INTEGRATION.md calls this
out as a hardening item.
"""

from __future__ import annotations

import logging
from typing import Any

from harness.connectors.base import ConnectorMethodError

logger = logging.getLogger("harness.connectors.postgres")


class PostgresConnector:
    def __init__(self, name: str, dsn: str):
        self.name = name
        self.dsn = dsn

    async def fetch(self, method: str, ref: Any) -> Any:
        import psycopg
        from psycopg.rows import dict_row
        sql = ref if isinstance(ref, str) else ref.get("sql")
        async with await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row) as conn:
            cur = await conn.execute(sql)
            if method == "query":
                rows = await cur.fetchall()
                logger.info("pg query → %d rows", len(rows))
                return rows
            if method == "query_one":
                return await cur.fetchone()
            raise ConnectorMethodError(f"postgres connector has no fetch method {method!r}")

    async def write(self, method: str, container: str | None, payload: Any) -> Any:
        import psycopg
        if method != "execute":
            raise ConnectorMethodError(f"postgres connector has no write method {method!r}")
        sql = payload if isinstance(payload, str) else payload.get("sql")
        params = None if isinstance(payload, str) else payload.get("params")
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            cur = await conn.execute(sql, params)
            await conn.commit()
            return {"rowcount": cur.rowcount}

    async def close(self) -> None:
        pass


__all__ = ["PostgresConnector"]
