# Harness image — one image, role by command (mirrors ADR-0011 "one image,
# role by config"). The same image runs the worker, the JupyterLab explorer,
# and the example MCP server; the compose file picks the role per service.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# System deps kept minimal; psycopg[binary] needs no libpq dev headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the engine + packages + demo glue
COPY harness/ ./harness/
COPY packages/ ./packages/
COPY mcp_servers/ ./mcp_servers/
COPY scripts/ ./scripts/
COPY notebooks/ ./notebooks/

# Default role: the Postgres SKIP LOCKED worker. Override `command:` per service.
CMD ["python", "-m", "harness.cli", "worker"]
