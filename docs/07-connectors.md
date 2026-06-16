# Module 07 — Connectors: Sources and Targets

> Key files: `harness/connectors/base.py`, `harness/connectors/binder.py`,
> `harness/connectors/postgres.py`, `harness/connectors/s3.py`
>
> This module covers how the binder resolves sources before the loop and writes
> targets after — the template engine, the connector protocol, both connector
> implementations, and the mock seams for each path.

---

## Where the binder sits

The binder is the translation layer between the package's declarative bindings
and the raw connector calls. The engine never touches a connector directly.

```
Package YAML declares:                Engine calls:
  sources:                              binder.resolve_sources(pkg.sources,
    - connector: pg_main                                       input_data, mock)
      method: query_one                   │
      ref: "SELECT * FROM ..."            │  for each SourceBinding:
      bind_to: submission                 │    ① template ref with input
                                          │    ② registry.get(connector_name)
  targets:                                │    ③ connector.fetch(method, ref)
    - connector: s3_main                  │    ④ store in context dict
      method: put_object                  │
      from_path: "$"              binder.write_targets(pkg.targets, output, ...)
      container: "uw/{{run_id}}"          │  for each TargetBinding:
                                          │    ① select value from output
                                          │    ② template container
                  ┌────────────────────── │    ③ connector.write(method, ...)
                  ▼                       │
         ConnectorRegistry                │
          ┌─────────────┐                 │
          │   pg_main   │◄─ PostgresConnector(dsn=...)
          │   s3_main   │◄─ S3Connector(endpoint=...)
          └─────────────┘
                  │
         real external systems
         (Postgres, MinIO/S3)
```

The mock seam sits inside the binder, before any connector call is made. When a
`MockContext` is present, the binder returns canned data or skips writes without the connector ever being called.

---

## What connectors do

A connector is an object that talks to one external system. Every connector
implements the same two-verb protocol:

```python
@runtime_checkable
class Connector(Protocol):
    name: str
    async def fetch(self, method: str, ref: Any) -> Any: ...
    async def write(self, method: str, container: str | None, payload: Any) -> Any: ...
    async def close(self) -> None: ...
```

`fetch` reads data in (a source). `write` sends data out (a target). `method`
selects which operation within the connector — `"query"` vs `"query_one"` for
Postgres, `"get_object"` vs `"get_bytes"` vs `"get_json"` for S3.

The binder sits above the connectors and translates package declarations into
connector calls. The engine calls the binder; the binder calls connectors. The
engine never imports `psycopg`, `boto3`, or any connector-specific library.

---

## The connector registry

```python
class ConnectorRegistry:
    def register(self, connector: Connector) -> None:
        self._connectors[connector.name] = connector

    def get(self, name: str) -> Connector:  # raises ConnectorError if missing
        ...
```

Connectors are registered at startup by the factory:

```python
# harness/core/factory.py — build_connectors()
if os.environ.get("PG_MAIN_DSN"):
    reg.register(PostgresConnector("pg_main", os.environ["PG_MAIN_DSN"]))
if os.environ.get("S3_ENDPOINT_URL"):
    reg.register(S3Connector("s3_main", endpoint_url=..., access_key=..., secret_key=...))
```

A connector is only registered when its environment variables are present. If
`S3_ENDPOINT_URL` is absent, `"s3_main"` is never in the registry. When a package
source declares `connector: s3_main` and the connector is missing, `registry.get`
raises `ConnectorError` — the binder catches this and either raises
`SourceResolutionError` (for `required: true`) or logs and continues (for
`required: false`).

This is why the `classify` demo fails with a clear error when MinIO is not
configured: "No connector registered for 's3_main'" — not a cryptic boto3 error.

---

## Source resolution — the binder's job

`Binder.resolve_sources` is called before the first model turn. It walks the
package's `sources` list and for each one:

```python
async def resolve_sources(self, sources, run_input, mock) -> tuple[dict, list[dict]]:
    context = {}
    audit = []
    scope = {"input": run_input}

    for src in sources:
        # 1. Mock seam
        if mock is not None and src.bind_to in mock.source_responses:
            context[src.bind_to] = mock.source_responses[src.bind_to]
            audit.append(_src_audit(src, value, mocked=True))
            continue

        try:
            # 2. Template the ref
            ref = _template(src.ref, scope)
            # 3. Fetch via the named connector
            connector = self.connectors.get(src.connector)
            value = await connector.fetch(src.method, ref)
            # 4. Store: text value or block-metadata dict
            if src.as_block and isinstance(value, bytes):
                mime, _ = mimetypes.guess_type(ref)
                context[src.bind_to] = {
                    "_as_block": src.as_block,
                    "_media_type": mime or "application/octet-stream",
                    "_data_b64": base64.b64encode(value).decode(),
                    "_title": src.bind_to,
                }
            else:
                context[src.bind_to] = value
            audit.append(_src_audit(src, value, mocked=False))
        except Exception as e:
            audit.append(_src_audit(src, None, mocked=False, error=str(e)))
            if src.required:
                raise SourceResolutionError(...) from e
            # else: log and continue

    return context, audit
```

The return value is `(context, audit)`:
- `context` maps `bind_to` names to resolved values; the engine merges this with
  `run_input` and passes it to the prompt template renderer
- `audit` is a list of dicts, one per source, added to the decision record

---

## Template engine

```python
_TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

def _template(value: Any, scope: dict) -> Any:
    if not isinstance(value, str):
        return value
    def repl(m):
        path = m.group(1)            # e.g. "input.submission_id"
        cur = scope
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return str(cur if cur is not None else "")
    return _TEMPLATE.sub(repl, value)
```

`_template` is used in two places:

1. **Source `ref` templating** — before the fetch call, with `scope = {"input": run_input}`:

   ```yaml
   ref: "SELECT * FROM submission WHERE id = {{input.submission_id}}"
   ```
   becomes
   ```sql
   SELECT * FROM submission WHERE id = 1
   ```

2. **Target `container` templating** — before the write call, with
   `scope = {"input": run_input, "output": output, "run_id": run_id}`:

   ```yaml
   container: "underwriting/{{run_id}}.json"
   ```
   becomes
   ```
   underwriting/3a7f1b2c-....json
   ```

The template engine is deliberately minimal — dotted path lookups into a
two-level scope. It is not Jinja2. `{{input.x}}` and `{{context.x}}` for the
prompt template (rendered by the engine); `{{input.x}}`, `{{output.x}}`,
`{{run_id}}` for connector refs and containers (rendered by the binder).

**Security note:** SQL is assembled by string substitution, not parameterized.
This is flagged in the Postgres connector's docstring as a hardening item for
production. In the current demo, the only interpolated values come from the run's
input dict (controlled by the application), not from user-supplied text, so the
risk is low but real. Production adoption should switch to parameterized queries.

---

## The `as_block` path: bytes to the model

For binary sources (`as_block: image` or `as_block: document`), the connector
returns raw `bytes` and the binder wraps them in a neutral block-metadata dict
instead of storing the bytes directly:

```python
if src.as_block and isinstance(value, bytes):
    mime, _ = mimetypes.guess_type(ref)  # "documents/complaint.txt" → "text/plain"
    context[src.bind_to] = {
        "_as_block": src.as_block,           # "image" or "document"
        "_media_type": mime or "application/octet-stream",
        "_data_b64": base64.b64encode(value).decode(),  # str, not bytes
        "_title": src.bind_to,
    }
```

The full journey from S3 bytes to a content block the model reads:

```
Package YAML:
  method: get_bytes
  as_block: document

         S3 object ("documents/complaint.txt")
                    │
                    ▼ bytes
         S3Connector.fetch("get_bytes", ref)
                    │ raw bytes
                    ▼
         binder: src.as_block is set AND value is bytes
                    │
                    ▼ mimetypes.guess_type("documents/complaint.txt")
                                       → "text/plain"
                    │
                    ▼ base64.b64encode(bytes).decode()
         context["document"] = {
           "_as_block":   "document",
           "_media_type": "text/plain",
           "_data_b64":   "U3ViamVjd...",  ← str, JSON-safe
           "_title":      "document",
         }
                    │
         engine._first_user_message() reads the dict
                    │
                    ▼
         DocumentBlock(media_type="text/plain", data_b64="U3ViamVjd...")
         appended to first user Message
                    │
         AnthropicProvider._to_anthropic_msg():
                    │
                    ▼
         {"type": "document",
          "source": {"type": "text", "media_type": "text/plain",
                     "data": "Subject: URGENT complaint..."}}
         sent to API
```

Why a dict rather than an IR block directly? The binder has no import from
`harness.core.ir` — keeping that dependency off the binder keeps the dependency
graph clean. The engine (which owns IR) reads the dict and creates the typed
`ImageBlock` or `DocumentBlock` in `_first_user_message`.

Why base64 str rather than raw bytes? The dict is audited in the decision log
via `_src_audit`. Decision records must be JSON-serialisable. Raw `bytes` are not
JSON-serialisable; a base64 `str` is.

---

## Target writes — after the loop

```python
async def write_targets(self, targets, output, run_input, run_id, mock) -> list[dict]:
    audit = []
    scope = {"input": run_input, "output": output, "run_id": run_id}
    for tgt in targets:
        value = _select(output, tgt.from_path)          # extract from output
        container = _template(tgt.container, scope)     # fill {{run_id}} etc.

        # Suppress seam
        if mock is not None and mock.suppress_targets:
            audit.append(_tgt_audit(tgt, container, handle=None, suppressed=True))
            continue

        # Replay seam
        if mock is not None and tgt.from_path in mock.target_handles:
            audit.append(_tgt_audit(tgt, container, handle=mock.target_handles[tgt.from_path], ...))
            continue

        try:
            connector = self.connectors.get(tgt.connector)
            handle = await connector.write(tgt.method, container, value)
            audit.append(_tgt_audit(tgt, container, handle=handle, suppressed=False))
        except Exception as e:
            audit.append(_tgt_audit(tgt, container, handle=None, error=str(e)))
            if tgt.required:
                raise TargetWriteError(...) from e
    return audit
```

### `_select` — JSONPath-ish output selector

```python
def _select(output: dict, from_path: str) -> Any:
    if from_path in ("$", ""):
        return output              # whole output dict
    path = from_path[2:] if from_path.startswith("$.") else from_path
    cur = output
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur
```

Common patterns:

| `from_path` | What is written |
|---|---|
| `$` | The whole output dict |
| `$.premium` | Just the `premium` field |
| `$.coverage_summary.limit` | A nested field |

Multiple targets with different `from_path` values let you fan one output out to
multiple destinations.

### `required` vs optional targets

```yaml
targets:
  - connector: s3_main
    method: put_object
    from_path: "$"
    container: "underwriting/{{run_id}}.json"
    required: false    # write failure does NOT fail the run
```

With `required: false`, a write failure is recorded in the audit log but the run
status shows `complete`. With `required: true`, the exception propagates up and
the run is marked `failed`.

Set `required: true` on any target a downstream consumer depends on. `required: false`
is only appropriate for genuinely optional artifacts where silent absence is
acceptable. The demo packages use `required: false` so they work in environments
without MinIO configured, but a production deployment where a consumer reads from
that S3 bucket should use `required: true`.

---

## The Postgres connector

```python
class PostgresConnector:
    async def fetch(self, method: str, ref: Any) -> Any:
        import psycopg           # lazy import
        from psycopg.rows import dict_row
        sql = ref if isinstance(ref, str) else ref.get("sql")
        async with await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row) as conn:
            cur = await conn.execute(sql)
            if method == "query":
                return await cur.fetchall()    # list[dict]
            if method == "query_one":
                return await cur.fetchone()    # dict | None
            raise ConnectorMethodError(...)
```

Two fetch methods:

| `method` | Returns | Use case |
|---|---|---|
| `query` | `list[dict]` — all matching rows | Multiple results |
| `query_one` | `dict` or `None` | Single row; returns `None` if no match |

The `underwriting_agent` uses `query_one` to fetch exactly the submission row for
the given `submission_id`. If the row does not exist, `context["submission"]` is
`None`, and the prompt template renders `{{context.submission}}` as the empty
string. With `required: true`, a `None` result from `query_one` is not itself an
error (the connector returned successfully); only a connector exception triggers
`SourceResolutionError`.

`psycopg` is imported lazily inside the method — not at module load time. This
means the offline/mock path (where `resolve_sources` hits the mock seam and
returns before ever calling `connector.fetch`) never needs the driver installed.

Write method:

```python
async def write(self, method, container, payload) -> Any:
    if method != "execute":
        raise ConnectorMethodError(...)
    sql = payload if isinstance(payload, str) else payload.get("sql")
    params = None if isinstance(payload, str) else payload.get("params")
    async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return {"rowcount": cur.rowcount}
```

The `execute` write method supports optional parameterized queries via the
`params` field, which is the correct production pattern. The demo's source
bindings use string-templated SQL (via the binder's `_template`), but target
writes can use parameterized SQL by passing `{"sql": "...", "params": [...]}` as
the payload.

---

## The S3 connector

```python
class S3Connector:
    def _ensure_client(self):
        if self._client is None:
            import boto3          # lazy import
            self._client = boto3.client("s3", endpoint_url=self._endpoint_url, ...)
        return self._client

    async def fetch(self, method: str, ref: Any) -> Any:
        import asyncio
        client = self._ensure_client()
        bucket, key = self._split(ref)   # "documents/complaint.txt" → ("documents", "complaint.txt")

        def _get() -> bytes:
            obj = client.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()

        raw: bytes = await asyncio.to_thread(_get)  # boto3 is synchronous
        if method == "get_object":  return raw.decode("utf-8")
        if method == "get_bytes":   return raw
        if method == "get_json":    return json.loads(raw.decode("utf-8"))
        raise ConnectorMethodError(...)
```

Three fetch methods:

| `method` | Returns | Use case |
|---|---|---|
| `get_object` | `str` (UTF-8 decoded) | Text objects for prompt context |
| `get_bytes` | `bytes` | Binary files (images, PDFs) — use with `as_block` |
| `get_json` | parsed `dict` / `list` | JSON objects |

**`asyncio.to_thread`:** boto3's S3 client is synchronous. Calling it directly
in an async function would block the event loop for the duration of the network
request. `asyncio.to_thread` runs the blocking call in a thread pool so the event
loop remains responsive. The same pattern is used for writes.

**`_ensure_bucket` on write:** Before a `put_object`, the connector calls
`head_bucket` to check whether the bucket exists, and creates it if not:

```python
@staticmethod
def _ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        try:
            client.create_bucket(Bucket=bucket)
        except Exception:
            pass    # bucket may have been created by a concurrent caller
```

This is a convenience for the demo (MinIO starts empty; buckets are created on
first write). In production, buckets are pre-created by infrastructure tooling
and this auto-create is unnecessary but harmless.

---

## Source audit records

Every source resolution (live or mocked) produces an audit dict:

```python
def _src_audit(src, value, *, mocked, error=None) -> dict:
    return {
        "bind_to": src.bind_to,
        "connector": src.connector,
        "method": src.method,
        "mocked": mocked,
        "error": error,
        "value_preview": _preview(value),  # first 200 chars of str(value)
    }
```

The `value_preview` is deliberately truncated. Full values from Postgres rows or
S3 objects can be large; storing them verbatim in the decision record would bloat
it. The preview is enough to confirm what was fetched; the full value can be
retrieved from the source system if needed.

---

## Mock seams

The binder has three mock paths:

**Source mock** (`MockContext.source_responses`):
```python
if mock is not None and src.bind_to in mock.source_responses:
    value = mock.source_responses[src.bind_to]
    context[src.bind_to] = value
    continue   # connector.fetch never called
```

**Target suppress** (`MockContext.suppress_targets`):
```python
if mock is not None and mock.suppress_targets:
    audit.append(_tgt_audit(tgt, container, handle=None, suppressed=True))
    continue   # connector.write never called
```

**Target replay** (`MockContext.target_handles`):
```python
if mock is not None and tgt.from_path in mock.target_handles:
    audit.append(_tgt_audit(tgt, container, handle=mock.target_handles[tgt.from_path], ...))
    continue   # returns a canned handle without writing
```

The `reproduce` CLI command uses all three: `model_replay` for the model,
`mock_all_tools=True` for tools, `suppress_targets=True` to avoid overwriting
the original S3 objects. The result is a bit-exact audit re-run that writes a
new decision record without any real side effects.

---

## Checkpoint

1. A package declares a source with `connector: s3_main`, `method: get_bytes`,
   `as_block: document`. What does `context["document"]` contain after
   `resolve_sources`? What type is it? Why not an `ImageBlock` or `DocumentBlock`?

2. `_template("SELECT * FROM submission WHERE id = {{input.submission_id}}", {"input": {"submission_id": 42}})`
   returns what string?

3. A target has `from_path: "$.premium"` and the output is
   `{"decision": "bound", "premium": 7438.18, "rationale": "..."}`. What value
   is written to the connector?

4. A required source (`required: true`) fails because `S3_ENDPOINT_URL` is not
   set and the `s3_main` connector is not in the registry. What exception is
   raised? When does the engine catch it, and what is the run's final status?

5. `mock.suppress_targets=True` is active. The target write audit record has
   `suppressed: true`. Is any data written to S3?

6. Both `get_object` and `get_bytes` call `client.get_object` on the S3 API.
   What is the difference in what they return?

When you can answer these, move to **[Module 08: HITL — Human in the Loop](08-hitl.md)**
— how the continuation is serialised, stored, and resumed after a human records a
decision.
