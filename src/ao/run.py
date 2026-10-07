"""Single agent run: resolve agent and backend, build the prompt, call, record.

This is the seed of the task runner (KAP-83); the full context builder is KAP-84.
"""

import hashlib
import sqlite3
from dataclasses import dataclass, field

from ao import budget, mcp
from ao.agents import Agent, agent_problem, load_agent
from ao.backends import get_backend
from ao.backends.base import Backend, Result, RunRequest
from ao.config import BudgetsConfig, LoadedConfig
from ao.db import repo
from ao.secrets import SecretNotFound


class RunError(Exception):
    """The run could not be prepared (bad agent, missing backend, ...)."""


@dataclass(frozen=True)
class PreparedRun:
    agent: Agent
    backend: Backend
    request: RunRequest
    global_budgets: BudgetsConfig = field(default_factory=BudgetsConfig)
    local_backends: frozenset[str] = frozenset()

    @property
    def prompt_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.request.system_prompt.encode())
        digest.update(b"\0")
        digest.update(self.request.prompt.encode())
        return digest.hexdigest()

    @property
    def estimated_tokens(self) -> int:
        """Rough size of what we send (chars/4). The backend may add its own overhead."""
        return (len(self.request.system_prompt) + len(self.request.prompt)) // 4


@dataclass(frozen=True)
class RunRecord:
    run_id: int
    result: Result


def build_system_prompt(agent: Agent) -> str:
    """Stable content first (instructions, then memory) so prompt caching can kick in."""
    parts = [agent.instructions]
    if agent.memory_index:
        parts.append(f"## Memory\n{agent.memory_index}")
    return "\n\n".join(parts)


def prepare(
    loaded: LoadedConfig, agent_name: str, prompt: str, *, attachments: list[str] | None = None
) -> PreparedRun:
    if attachments:
        raise RunError("attachments are not supported yet (KAP-84)")
    agent = load_agent(loaded, agent_name)
    if problem := agent_problem(loaded, agent):
        raise RunError(f"agent {agent_name!r} cannot run: {problem}")
    if not prompt.strip():
        raise RunError("empty prompt")
    if agent.mcp_config is not None:
        try:
            mcp.check_secrets(agent.mcp_config)
        except SecretNotFound as exc:
            raise RunError(f"{agent.mcp_config}: {exc}") from exc
    backend_config = loaded.config.backends[agent.config.backend]
    backend = get_backend(agent.config.backend, backend_config)
    request = RunRequest(
        agent=agent,
        system_prompt=build_system_prompt(agent),
        prompt=prompt,
        model=agent.config.model or backend_config.model,
        max_cost_usd=agent.config.budget.per_run_cost_usd,
    )
    return PreparedRun(
        agent=agent,
        backend=backend,
        request=request,
        global_budgets=loaded.config.budgets,
        local_backends=frozenset(n for n, b in loaded.config.backends.items() if b.is_local),
    )


def check_budget(conn: sqlite3.Connection, prepared: PreparedRun) -> budget.Decision:
    return budget.check(
        conn,
        prepared.agent.name,
        prepared.agent.config.budget,
        backend_is_local=prepared.backend.is_local,
        global_budgets=prepared.global_budgets,
        local_backends=prepared.local_backends,
    )


def execute(
    conn: sqlite3.Connection,
    prepared: PreparedRun,
    task_id: int | None = None,
    *,
    force: bool = False,
) -> RunRecord:
    """Check budgets, run the backend, persist a `runs` row plus an event.

    Over budget: the run is recorded as `refused` and the backend is never called,
    unless `force` is set (which is logged as a `budget.override` event).
    """
    decision = check_budget(conn, prepared)
    if not decision.allowed:
        if not force:
            return _record(conn, prepared, Result(outcome="refused", error=decision.reason),
                           task_id, event="run.refused")  # fmt: skip
        repo.log_event(conn, "budget.override", agent=prepared.agent.name, task_id=task_id,
                       data={"reason": decision.reason})  # fmt: skip
    prepared.agent.workspace.mkdir(parents=True, exist_ok=True)
    return _record(conn, prepared, prepared.backend.run(prepared.request), task_id)


def _record(
    conn: sqlite3.Connection,
    prepared: PreparedRun,
    result: Result,
    task_id: int | None,
    event: str = "run.finished",
) -> RunRecord:
    usage = result.usage
    run_id = repo.record_run(
        conn,
        agent=prepared.agent.name,
        backend=prepared.backend.name,
        outcome=result.outcome,
        task_id=task_id,
        model=result.model or prepared.request.model,
        prompt_hash=prepared.prompt_hash,
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        duration_ms=result.duration_ms,
        session_id=result.session_id,
        num_turns=result.num_turns,
        error=result.error,
    )
    repo.log_event(
        conn,
        event,
        agent=prepared.agent.name,
        task_id=task_id,
        run_id=run_id,
        data={"outcome": result.outcome, "backend": prepared.backend.name},
    )
    return RunRecord(run_id=run_id, result=result)
