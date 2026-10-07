"""`ao memory compact`: one cheap structured call that merges and prunes an agent's facts."""

import sqlite3
from dataclasses import dataclass
from typing import Any

from ao import memory, run
from ao.agents import load_agent
from ao.config import LoadedConfig

INSTRUCTIONS = """You maintain another agent's long-term memory, a list of one-line facts.
Merge duplicates and near-duplicates, drop stale, trivial or contradicted facts, and keep
each fact one short line. Keep an existing id when its fact survives. Never invent facts."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
            },
        }
    },
    "required": ["facts"],
}


class CompactionError(Exception):
    pass


@dataclass(frozen=True)
class Proposal:
    before: list[memory.Fact]
    after: list[memory.Fact]
    run_id: int

    def diff(self) -> list[str]:
        old = {f.id: f.text for f in self.before}
        new = {f.id: f.text for f in self.after}
        lines = [f"- [{i}] {t}" for i, t in old.items() if new.get(i) != t]
        lines += [f"+ [{i}] {t}" for i, t in new.items() if old.get(i) != t]
        return lines


def propose(
    conn: sqlite3.Connection, loaded: LoadedConfig, agent_name: str, model: str | None = None
) -> Proposal:
    prepared_agent = load_agent(loaded, agent_name)
    before = memory.load(prepared_agent.dir)
    if not before:
        raise CompactionError(f"agent {agent_name!r} has no memory to compact")
    target = max(1, prepared_agent.config.memory.max_facts // 2)
    prompt = f"Compact these facts to at most {target}:\n{memory.render(before)}"
    backend_type = loaded.config.backends[prepared_agent.config.backend].type
    prepared = run.prepare(
        loaded, agent_name, prompt, output_schema=SCHEMA, instructions=INSTRUCTIONS,
        model=model or ("haiku" if backend_type == "claude_code" else None), writeback=False,
    )  # fmt: skip
    record = run.execute(conn, prepared)
    if record.result.outcome != "ok" or record.result.structured is None:
        raise CompactionError(f"compaction run failed: {record.result.error}")
    after, seen = [], set()
    for item in record.result.structured.get("facts", []):
        fact_id = memory.normalize_id(str(item.get("id", "")))
        text = memory.clean_text(str(item.get("text", "")))
        if memory.ID_RE.match(fact_id) and text and fact_id not in seen:
            seen.add(fact_id)
            after.append(memory.Fact(fact_id, text))
    return Proposal(before=before, after=after, run_id=record.run_id)
