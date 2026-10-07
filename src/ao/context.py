"""Context builder: the smallest prompt that does the job, in cache-friendly order.

System part (stable, cacheable):  instructions → memory → extra system sections
                                  (e.g. Jarvis's roster, memory protocol)
Prompt part (varies per run):     attached documents → the task itself

Only what is explicitly attached is included. There is no implicit company-wide context.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ao.agents import Agent

MAX_ATTACHMENT_BYTES = 200_000


class ContextError(Exception):
    pass


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic estimate (~4 chars per token)."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class Section:
    name: str
    part: Literal["system", "prompt"]
    text: str

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class Context:
    sections: tuple[Section, ...]

    def _join(self, part: str) -> str:
        return "\n\n".join(s.text for s in self.sections if s.part == part)

    @property
    def system_prompt(self) -> str:
        return self._join("system")

    @property
    def prompt(self) -> str:
        return self._join("prompt")

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.system_prompt) + estimate_tokens(self.prompt)


def read_attachment(path: Path) -> str:
    path = path.expanduser()
    if not path.is_file():
        raise ContextError(f"attachment not found: {path}")
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise ContextError(f"attachment too large: {path} ({size:,} > {MAX_ATTACHMENT_BYTES:,} B)")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContextError(f"attachment is not UTF-8 text: {path}") from exc


def build(
    agent: Agent,
    prompt: str,
    attachments: Iterable[str | Path] = (),
    extra_system: Sequence[tuple[str, str]] = (),
) -> Context:
    sections = [Section("instructions", "system", agent.instructions)]
    if agent.memory_index:
        sections.append(Section("memory", "system", f"## Memory\n{agent.memory_index}"))
    sections += [Section(name, "system", text) for name, text in extra_system if text]
    for raw in attachments:
        path = Path(raw)
        body = read_attachment(path).strip()
        sections.append(
            Section(f"attachment:{path.name}", "prompt",
                    f'<document path="{path}">\n{body}\n</document>')
        )  # fmt: skip
    sections.append(Section("task", "prompt", prompt.strip()))
    return Context(tuple(sections))


def check_limit(context: Context, limit: int) -> None:
    if context.est_tokens > limit:
        raise ContextError(
            f"context is ~{context.est_tokens:,} tokens, over this agent's limit of {limit:,}"
            " (limits.max_context_tokens); attach less or raise the limit"
        )


def describe(context: Context, limit: int | None = None) -> str:
    """Per-section token table for --dry-run."""
    width = max(len(s.name) for s in context.sections)
    lines = ["# context (~tokens, chars/4):"]
    lines += [f"#   {s.part:<6}  {s.name:<{width}}  {s.est_tokens:>6,}" for s in context.sections]
    total = f"#   {'total':<6}  {'':<{width}}  {context.est_tokens:>6,}"
    lines.append(total + (f"  (limit {limit:,})" if limit else ""))
    return "\n".join(lines)
