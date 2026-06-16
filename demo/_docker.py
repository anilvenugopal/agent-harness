# SPDX-License-Identifier: AGPL-3.0-or-later
"""Docker Compose helpers for the demo CLI."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_COMPOSE = [
    "docker", "compose",
    "--project-directory", str(_ROOT),
    "-f", str(_ROOT / "docker" / "docker-compose.yml"),
]

SERVICES = ["postgres", "minio", "mcp-server", "worker", "jupyter"]


def compose_json(*args) -> list[dict]:
    """Run a compose command and return parsed JSON output (array or NDJSON)."""
    result = subprocess.run([*_COMPOSE, *args], capture_output=True, text=True)
    out = result.stdout.strip()
    if not out:
        return []
    try:
        parsed = json.loads(out)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows


def compose_stream(*args):
    """Run a compose command and yield output lines as they arrive.

    Terminates the subprocess on GeneratorExit or KeyboardInterrupt so
    `docker compose logs --follow` doesn't linger as an orphan.
    """
    proc = subprocess.Popen(
        [*_COMPOSE, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for line in proc.stdout:
            yield line.rstrip()
        proc.wait()
    except (GeneratorExit, KeyboardInterrupt):
        proc.terminate()
        proc.wait()
        raise
