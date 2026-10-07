"""Backend interface shared by every model provider."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ao.agents import Agent
from ao.config import BackendConfig

# "refused" is set by the runner (budget), never by a backend.
Outcome = Literal["ok", "error", "timeout", "refused"]


class BackendError(Exception):
    """Backend cannot be constructed (unknown or unimplemented type)."""


@dataclass(frozen=True)
class RunRequest:
    agent: Agent
    system_prompt: str
    prompt: str
    model: str | None = None
    json_schema: dict[str, Any] | None = None
    max_cost_usd: float | None = None
    # Conversation continuity (backends with sessions): keep the session, or resume one.
    persist_session: bool = False
    resume_session_id: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

    @property
    def billable_tokens(self) -> int:
        """Tokens counted against budgets; cache reads are cheap and excluded."""
        return self.input_tokens + self.cache_write_tokens + self.output_tokens


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    session_id: str | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None
    structured: dict[str, Any] | None = None  # set when the request had a json_schema


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str


class Backend(Protocol):
    name: str
    config: BackendConfig

    @property
    def is_local(self) -> bool: ...

    def run(self, request: RunRequest) -> Result: ...

    def health(self) -> Health: ...

    def preview(self, request: RunRequest) -> str:
        """Human-readable description of what `run` would execute (for --dry-run)."""
        ...
