# SPDX-License-Identifier: AGPL-3.0-or-later
"""S3 / object-store connector — Category B object access.

Methods:
  fetch("get_object", "bucket/key")     → decoded text (UTF-8) of the object
  fetch("get_json", "bucket/key")       → parsed JSON
  write("put_object", "bucket/key", payload) → {"bucket","key","etag"}

Targets MinIO in the demo (S3-compatible, ADR-0015/0018), and any real S3 in
production — same boto3 client, just a different endpoint_url. boto3 is
imported lazily so the offline/mock path needs no AWS SDK.

`container` for a write is "bucket/key". The binder fills {{run_id}} etc.
before calling, so the connector just splits and puts.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from harness.connectors.base import ConnectorMethodError

logger = logging.getLogger("harness.connectors.s3")


class S3Connector:
    def __init__(
        self,
        name: str,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "us-east-1",
    ):
        self.name = name
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            import boto3  # lazy
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
            )
        return self._client

    @staticmethod
    def _split(container: str) -> tuple[str, str]:
        bucket, _, key = container.partition("/")
        return bucket, key

    async def fetch(self, method: str, ref: Any) -> Any:
        import asyncio
        client = self._ensure_client()
        bucket, key = self._split(ref if isinstance(ref, str) else ref["container"])

        def _get() -> bytes:
            obj = client.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()

        raw: bytes = await asyncio.to_thread(_get)
        if method == "get_object":
            return raw.decode("utf-8")
        if method == "get_bytes":
            return raw          # caller receives raw bytes for image/document blocks
        if method == "get_json":
            return json.loads(raw.decode("utf-8"))
        raise ConnectorMethodError(f"s3 connector has no fetch method {method!r}")

    async def write(self, method: str, container: str | None, payload: Any) -> Any:
        import asyncio
        if method != "put_object":
            raise ConnectorMethodError(f"s3 connector has no write method {method!r}")
        client = self._ensure_client()
        bucket, key = self._split(container)
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload, default=str)
        if isinstance(body, str):
            body = body.encode("utf-8")

        def _put():
            self._ensure_bucket(client, bucket)
            resp = client.put_object(Bucket=bucket, Key=key, Body=body)
            return {"bucket": bucket, "key": key, "etag": resp.get("ETag", "").strip('"')}

        handle = await asyncio.to_thread(_put)
        logger.info("s3 put → %s/%s", bucket, key)
        return handle

    @staticmethod
    def _ensure_bucket(client, bucket: str) -> None:
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            try:
                client.create_bucket(Bucket=bucket)
            except Exception:
                pass

    async def close(self) -> None:
        pass


__all__ = ["S3Connector"]
