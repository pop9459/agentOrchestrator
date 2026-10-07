"""Single agent run: resolve agent and backend, build the prompt, call, record.

This is the seed of the task runner (KAP-83); the full context builder is KAP-84.
"""

import copy
import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ao import budget, context, mcp, memory
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
    context: context.Context
    writeback: bool = False
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
        return self.context.est_tokens


@dataclass(frozen=True)
class RunRecord:
    run_id: int
    result: Result


# Default structured output when an agent has memory write-back but no schema of its own.
REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}


def with_memory_ops(schema: dict[str, Any]) -> dict[str, Any]:
    """Extend an object schema with the `memory` ops field (required, may be empty)."""
    extended = copy.deepcopy(schema)
    extended.setdefault("properties", {})["memory"] = memory.OPS_SCHEMA
    extended["required"] = [*extended.get("required", []), "memory"]
    return extended


def prepare(
    loaded: LoadedConfig,
    agent_name: str,
    prompt: str,
    *,
    attachments: Sequence[str] = (),
    extra_system: Sequence[tuple[str, str]] = (),
    output_schema: dict[str, Any] | None = None,
    instructions: str | None = None,
    model: str | None = None,
    writeback: bool | None = None,
) -> PreparedRun:
    """Resolve agent and backend and assemble the context. Sends nothing.

    `output_schema` requests structured output; a string `reply` field becomes the
    result text. `instructions` replaces the agent's persona (and drops its memory) for
    utility runs such as memory compaction; `model` and `writeback` override the agent.
    """
    agent = load_agent(loaded, agent_name)
    if instructions is not None:
        agent = replace(agent, instructions=instructions, memory_index=None)
    if problem := agent_problem(loaded, agent):
        raise RunError(f"agent {agent_name!r} cannot run: {problem}")
    if not prompt.strip():
        raise RunError("empty prompt")
    if agent.mcp_config is not None:
        try:
            mcp.check_secrets(agent.mcp_config)
        except SecretNotFound as exc:
            raise RunError(f"{agent.mcp_config}: {exc}") from exc
    writeback = agent.config.memory.writeback if writeback is None else writeback
    extra = list(extra_system)
    if writeback:
        extra.append(("memory-protocol", memory.PROTOCOL))
        output_schema = with_memory_ops(output_schema or REPLY_SCHEMA)
    try:
        ctx = context.build(agent, prompt, attachments, extra)
        context.check_limit(ctx, agent.config.limits.max_context_tokens)
    except context.ContextError as exc:
        raise RunError(str(exc)) from exc
    backend_config = loaded.config.backends[agent.config.backend]
    backend = get_backend(agent.config.backend, backend_config)
    request = RunRequest(
        agent=agent,
        system_prompt=ctx.system_prompt,
        prompt=ctx.prompt,
        model=model or agent.config.model or backend_config.model,
        max_cost_usd=agent.config.budget.per_run_cost_usd,
        json_schema=output_schema,
    )
    return PreparedRun(
        agent=agent,
        backend=backend,
        request=request,
        context=ctx,
        writeback=writeback,
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
    result = _structured(prepared, prepared.backend.run(prepared.request))
    record = _record(conn, prepared, result, task_id)
    if result.outcome == "ok" and prepared.writeback and result.structured is not None:
        _apply_memory(conn, prepared, result.structured.get("memory") or [], record, task_id)
    return record


def _structured(prepared: PreparedRun, result: Result) -> Result:
    """Enforce structured output when it was requested; surface `reply` as the text."""
    if result.outcome != "ok" or prepared.request.json_schema is None:
        return result
    if result.structured is None:
        return replace(result, outcome="error", error="expected structured output, got none")
    reply = result.structured.get("reply")
    return replace(result, text=reply) if isinstance(reply, str) else result


def _apply_memory(
    conn: sqlite3.Connection,
    prepared: PreparedRun,
    ops: list[Any],
    record: "RunRecord",
    task_id: int | None,
) -> None:
    if not ops:
        return
    agent = prepared.agent
    outcome = memory.apply(memory.load(agent.dir), ops, agent.config.memory.max_facts)
    if outcome.applied:
        memory.save(agent.dir, outcome.facts)
    for op in outcome.applied:
        repo.log_event(conn, f"memory.{op.op}", agent=agent.name, task_id=task_id,
                       run_id=record.run_id, data={"id": op.id, "text": op.text})  # fmt: skip
    for raw, reason in outcome.rejected:
        repo.log_event(conn, "memory.rejected", agent=agent.name, task_id=task_id,
                       run_id=record.run_id, data={"op": raw, "reason": reason})  # fmt: skip


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
        data={
            "outcome": result.outcome,
            "backend": prepared.backend.name,
            "permission_denials": len((result.raw or {}).get("permission_denials") or []),
        },
    )
    return RunRecord(run_id=run_id, result=result)
