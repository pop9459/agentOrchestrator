"""Agent memory: `agents/<name>/memory/INDEX.md`, one fact per line: `- [id] text`.

Only the index exists and only the index is sent, so memory stays small and its cost is
visible in `--dry-run`. Agents never write the file themselves. With
`[memory] writeback = true` they return memory ops in their structured output, and
`apply()` validates and applies them (KAP-85).
"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
LINE_RE = re.compile(r"^- \[([^\]]+)\] (.+)$")
MAX_TEXT = 300

PROTOCOL = """## Memory protocol
Your memory (above) persists between runs. Put durable facts worth remembering (user
preferences, conventions, recurring context) in the `memory` field as ops:
add {id, text} / update {id, text} / delete {id}. Ids are short lowercase slugs; text is
one line. Never store secrets or one-off details. Usually `memory` is an empty list."""

OPS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add", "update", "delete"]},
            "id": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["op", "id"],
    },
}


@dataclass(frozen=True)
class Fact:
    id: str
    text: str

    def line(self) -> str:
        return f"- [{self.id}] {self.text}"


@dataclass(frozen=True)
class Op:
    op: Literal["add", "update", "delete"]
    id: str
    text: str = ""


@dataclass(frozen=True)
class Applied:
    facts: list[Fact]
    applied: list[Op]
    rejected: list[tuple[dict[str, Any], str]]


def index_path(agent_dir: Path) -> Path:
    return agent_dir / "memory" / "INDEX.md"


def normalize_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", raw.strip().lower()).strip("-_")[:48]


def clean_text(raw: str) -> str:
    return " ".join(raw.split())[:MAX_TEXT]


def load(agent_dir: Path) -> list[Fact]:
    """Parse the index. Hand-written lines without an id get `note-N` ids."""
    path = index_path(agent_dir)
    if not path.is_file():
        return []
    facts, seen = [], set()
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE_RE.match(line)
        fact_id, text = (normalize_id(match[1]), match[2]) if match else (f"note-{n}", line)
        if fact_id and fact_id not in seen:
            seen.add(fact_id)
            facts.append(Fact(fact_id, clean_text(text.lstrip("- "))))
    return facts


def render(facts: list[Fact]) -> str:
    return "".join(f"{f.line()}\n" for f in facts)


def save(agent_dir: Path, facts: list[Fact]) -> None:
    """Atomic replace, so a crash never leaves a half-written index."""
    path = index_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".INDEX.", suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        handle.write(render(facts))
    os.replace(tmp, path)


def apply(facts: list[Fact], raw_ops: list[Any], max_facts: int) -> Applied:
    """Validate and apply ops in order; invalid ops are rejected with a reason, never raised."""
    current = {f.id: f for f in facts}
    order = [f.id for f in facts]
    applied: list[Op] = []
    rejected: list[tuple[dict[str, Any], str]] = []
    for raw in raw_ops:
        if not isinstance(raw, dict) or raw.get("op") not in ("add", "update", "delete"):
            rejected.append((raw if isinstance(raw, dict) else {"op": raw}, "invalid op"))
            continue
        fact_id = normalize_id(str(raw.get("id", "")))
        text = clean_text(str(raw.get("text", "")))
        if not ID_RE.match(fact_id):
            rejected.append((raw, "invalid id"))
            continue
        if raw["op"] == "delete":
            if fact_id not in current:
                rejected.append((raw, "unknown id"))
                continue
            del current[fact_id]
            order.remove(fact_id)
        else:
            if not text:
                rejected.append((raw, "empty text"))
                continue
            if fact_id not in current and len(current) >= max_facts:
                rejected.append((raw, f"memory full ({max_facts} facts); compact it"))
                continue
            if fact_id not in current:
                order.append(fact_id)
            current[fact_id] = Fact(fact_id, text)
        applied.append(Op(raw["op"], fact_id, text))
    return Applied([current[i] for i in order], applied, rejected)
