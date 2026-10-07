"""`ao hire`: the hiring agent designs a new agent; nothing is written without approval."""

import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ao import run
from ao.agents import (
    NAME_RE,
    AgentConfig,
    AgentError,
    agent_dir,
    create_agent,
    list_agent_names,
    load_agent,
)
from ao.config import LoadedConfig, format_validation_error

HIRING = "hiring"
CLAUDE_MODELS = "haiku | sonnet | opus"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "role": {"type": "string"},
        "backend": {"type": "string"},
        "model": {"type": "string", "description": "empty string = backend default"},
        "clearance": {"type": "string", "enum": ["public", "confidential"]},
        "prompt_mode": {"type": "string", "enum": ["replace", "append"]},
        "tools": {"type": "array", "items": {"type": "string"}},
        "max_turns": {"type": "integer"},
        "daily_tokens": {"type": "integer"},
        "instructions": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["name", "role", "backend", "model", "clearance", "prompt_mode", "tools",
                 "max_turns", "daily_tokens", "instructions", "reasoning"],
}  # fmt: skip


class HiringError(Exception):
    pass


@dataclass(frozen=True)
class Proposal:
    name: str
    config: AgentConfig
    instructions: str
    reasoning: str
    warnings: list[str]
    run_id: int


def catalogue(loaded: LoadedConfig) -> str:
    """What the hiring agent may choose from: existing agents and configured backends."""
    lines = ["## Existing agents (names are taken)"]
    for name in list_agent_names(loaded):
        try:
            lines.append(f"- {name}: {load_agent(loaded, name).config.role}")
        except AgentError:
            lines.append(f"- {name}")
    lines.append("\n## Backends you can choose")
    has_local = False
    for name, backend in loaded.config.backends.items():
        where = "local, may handle confidential data" if backend.is_local else "cloud"
        models = CLAUDE_MODELS if backend.type == "claude_code" else backend.model or "default"
        lines.append(f"- {name}: {backend.type} ({where}); models: {models}")
        has_local |= backend.is_local
    if not has_local:
        lines.append(
            '- (no local backend configured yet; for confidential work still use backend "local"'
            " and say in reasoning that it must be configured)"
        )
    return "\n".join(lines)


def ensure_hiring_agent(loaded: LoadedConfig) -> bool:
    """Create the hiring agent from its template if missing; True if created."""
    if agent_dir(loaded, HIRING).exists():
        return False
    create_agent(loaded, HIRING)
    return True


def _to_config(data: dict[str, Any]) -> AgentConfig:
    raw = {
        "role": data.get("role", ""),
        "backend": data.get("backend", ""),
        "model": data.get("model") or None,
        "clearance": data.get("clearance", "public"),
        "prompt": {"mode": data.get("prompt_mode", "replace")},
        "tools": {"builtin": list(data.get("tools") or [])},
        "limits": {"max_turns": data.get("max_turns", 4)},
        "budget": {"daily_tokens": data.get("daily_tokens")},
    }
    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise HiringError(format_validation_error(exc, title="Invalid proposal:")) from exc


def _warnings(loaded: LoadedConfig, config: AgentConfig) -> list[str]:
    warnings = []
    backend = loaded.config.backends.get(config.backend)
    if backend is None:
        warnings.append(f"backend {config.backend!r} is not configured; the agent can't run "
                        "until it is (see ao.example.toml)")  # fmt: skip
    elif config.clearance == "confidential" and not backend.is_local:
        warnings.append("confidential agent on a cloud backend: the runner will refuse "
                        "its confidential tasks")  # fmt: skip
    if config.tools.builtin:
        warnings.append(f"uses tools {config.tools.builtin}: ~4k+ input tokens per call")
    return warnings


def propose(conn: sqlite3.Connection, loaded: LoadedConfig, need: str) -> Proposal:
    taken = set(list_agent_names(loaded))
    extra = [("catalogue", catalogue(loaded))]
    prompt = f"Need: {need.strip()}"
    for attempt in range(2):  # one re-ask on a name collision
        prepared = run.prepare(loaded, HIRING, prompt, extra_system=extra, output_schema=SCHEMA)
        record = run.execute(conn, prepared)
        if record.result.outcome != "ok" or record.result.structured is None:
            raise HiringError(f"hiring run failed: {record.result.error}")
        data = record.result.structured
        name = str(data.get("name", "")).strip().lower()
        if not NAME_RE.match(name):
            raise HiringError(f"proposed name {name!r} is invalid")
        if name in taken:
            if attempt == 0:
                prompt += f"\n\nThe name {name!r} is taken; choose another."
                continue
            raise HiringError(f"proposed name {name!r} is already taken")
        config = _to_config(data)
        instructions = str(data.get("instructions", "")).strip()
        if not instructions:
            raise HiringError("proposal has no instructions")
        return Proposal(name, config, instructions, str(data.get("reasoning", "")).strip(),
                        _warnings(loaded, config), record.run_id)  # fmt: skip
    raise HiringError("no usable proposal")  # pragma: no cover
