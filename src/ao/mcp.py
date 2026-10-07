"""Per-agent MCP config (`agents/<name>/mcp.json`).

Claude Code always runs with `--strict-mcp-config`, so an agent sees only the servers in
its own mcp.json. Leaving that out loads every server the user has configured. With
two claude.ai connectors that was ~30k extra input tokens per run (KAP-79).

Secrets never live in mcp.json: write `{{secret:name}}` and ao substitutes the value
(see `ao.secrets`) into a private temp copy that exists only for the duration of a run.
"""

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ao import secrets

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_.-]{1,64})\}\}")


class McpConfigError(Exception):
    pass


def validate(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path}: invalid JSON: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise McpConfigError(f'{path}: expected {{"mcpServers": {{...}}}}')
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise McpConfigError(f"{path}: server {name!r} must be an object")
    return data


def secret_names(path: Path) -> set[str]:
    return set(PLACEHOLDER.findall(path.read_text()))


def check_secrets(path: Path) -> None:
    """Raise SecretNotFound for the first placeholder that cannot be resolved."""
    for name in sorted(secret_names(path)):
        secrets.get_secret(name)


def render(path: Path) -> str:
    """mcp.json text with placeholders replaced by JSON-escaped secret values."""

    def substitute(match: re.Match[str]) -> str:
        return json.dumps(secrets.get_secret(match[1]))[1:-1]

    return PLACEHOLDER.sub(substitute, path.read_text())


@contextmanager
def materialize(path: Path | None) -> Iterator[Path | None]:
    """Yield a path for `--mcp-config`: the file itself when it holds no placeholders,
    otherwise a 0600 temp copy with secrets filled in, deleted afterwards."""
    if path is None or not secret_names(path):
        yield path
        return
    fd, tmp = tempfile.mkstemp(prefix="ao-mcp-", suffix=".json")  # mode 0600
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(render(path))
        yield Path(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)
