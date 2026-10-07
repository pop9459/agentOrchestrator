"""J.A.R.V.I.S: answers directly or delegates to other agents, then summarises.

One request = one parent task for `jarvis`. Each Jarvis call returns structured output
`{reply, delegations[]}`. Delegations become child tasks that the runner executes right
away (auto-run; budgets, attempt and confidentiality guards still apply). Their results
go back to Jarvis for the final reply. At most MAX_ROUNDS delegation rounds per request.

Jarvis sees only the roster (name, role, backend/model, clearance), never other agents'
instructions or memory.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ao import run, runner
from ao.agents import AgentError, agent_problem, list_agent_names, load_agent
from ao.backends.base import BackendError
from ao.config import LoadedConfig
from ao.db import repo

JARVIS = "jarvis"
NOT_DELEGABLE = {JARVIS, "hiring"}  # hiring needs an interactive confirmation
MAX_DELEGATIONS = 4
MAX_ROUNDS = 2
RESULT_CHARS = 2000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "delegations": {
            "type": "array",
            "maxItems": MAX_DELEGATIONS,
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "title": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["agent", "title", "task"],
            },
        },
    },
    "required": ["reply", "delegations"],
}

PROTOCOL = f"""## How to respond
Answer in `reply`. If you can answer yourself, leave `delegations` empty. Delegate only
when a listed agent is clearly better suited (at most {MAX_DELEGATIONS} at once). Each
delegation has a short `title` and a self-contained `task`: the agent sees nothing else.
When delegations run you receive their results; then write the final `reply` for the user.
Only delegate to agents listed above."""


class JarvisError(Exception):
    pass


@dataclass
class Turn:
    reply: str
    task_id: int
    session_id: str | None
    delegated: list[runner.TaskOutcome] = field(default_factory=list)
    error: str | None = None


Event = Callable[[str, dict[str, Any]], None]


def roster(loaded: LoadedConfig) -> list[tuple[str, str]]:
    """(name, description) of every runnable agent Jarvis may delegate to."""
    entries = []
    for name in list_agent_names(loaded):
        if name in NOT_DELEGABLE:
            continue
        try:
            agent = load_agent(loaded, name)
        except AgentError:
            continue
        if agent_problem(loaded, agent):
            continue
        cfg = agent.config
        backend = loaded.config.backends[cfg.backend]
        model = cfg.model or backend.model or "default"
        where = "local" if backend.is_local else "cloud"
        entries.append((name, f"{cfg.role} ({cfg.backend}/{model}, {where}, {cfg.clearance})"))
    return entries


def roster_section(entries: list[tuple[str, str]]) -> str:
    lines = [f"- {name}: {desc}" for name, desc in entries] or ["- (no other agents yet)"]
    return "## Agents you can delegate to\n" + "\n".join(lines)


def _results_prompt(
    outcomes: list[runner.TaskOutcome], problems: list[str], *, final: bool, request: str | None
) -> str:
    parts = []
    if request is not None:  # no session: Jarvis needs the original request again
        parts.append(f"Original request:\n{request}")
    lines = ["Delegation results:"]
    for outcome in outcomes:
        task = outcome.task
        head = f"### #{task.id} {task.agent}: {task.title} [{task.status}]"
        body = (task.result or "")[:RESULT_CHARS] if task.status == "done" else task.error or ""
        lines.append(f"{head}\n{body}".rstrip())
    lines += [f"- not delegated: {problem}" for problem in problems]
    parts.append("\n\n".join(lines))
    parts.append(
        "Write the final reply now; do not delegate again."
        if final
        else "Write the final reply for the user (delegate again only if essential)."
    )
    return "\n\n".join(parts)


def _validate(raw: list[Any], allowed: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    valid, problems = [], []
    for item in raw:
        if not isinstance(item, dict):
            problems.append(f"malformed delegation {item!r}")
            continue
        agent, title, task = (str(item.get(k, "")).strip() for k in ("agent", "title", "task"))
        if agent not in allowed:
            problems.append(f"agent {agent!r} is not available")
        elif not task:
            problems.append(f"empty task for {agent!r}")
        elif len(valid) >= MAX_DELEGATIONS:
            problems.append(f"over the limit of {MAX_DELEGATIONS} delegations: {title!r}")
        else:
            valid.append({"agent": agent, "title": title or task[:60], "task": task})
    return valid, problems


def handle(
    conn: sqlite3.Connection,
    loaded: LoadedConfig,
    request: str,
    *,
    session_id: str | None = None,
    persist_session: bool = False,
    force: bool = False,
    on_event: Event | None = None,
) -> Turn:
    """Process one user request end to end. With `persist_session` the Claude session
    carries the conversation (chat); `session_id` resumes an existing one."""
    emit = on_event or (lambda kind, data: None)
    entries = roster(loaded)
    allowed = {name for name, _ in entries}
    extra = [("roster", roster_section(entries)), ("protocol", PROTOCOL)]
    title = " ".join(request.split())[:80] or "request"
    parent = repo.add_task(conn, title, request, agent=JARVIS)
    repo.start_task(conn, parent.id)
    turn = Turn(reply="", task_id=parent.id, session_id=session_id)

    prompt = request
    for round_no in range(MAX_ROUNDS + 1):
        try:
            prepared = run.prepare(
                loaded, JARVIS, prompt, extra_system=extra, output_schema=SCHEMA,
                persist_session=persist_session, resume_session_id=turn.session_id,
            )  # fmt: skip
        except (AgentError, BackendError, run.RunError) as exc:
            return _finish(conn, turn, error=str(exc))
        record = run.execute(conn, prepared, parent.id, force=force)
        result = record.result
        if result.outcome != "ok":
            return _finish(conn, turn, error=result.error or result.outcome)
        if persist_session and result.session_id:
            turn.session_id = result.session_id
        turn.reply = result.text
        raw = (result.structured or {}).get("delegations") or []
        if not raw or round_no == MAX_ROUNDS:
            return _finish(conn, turn)

        valid, problems = _validate(raw, allowed)
        outcomes = []
        for item in valid:
            child = repo.add_task(conn, item["title"], item["task"], agent=item["agent"],
                                  parent_id=parent.id, depth=parent.depth + 1)  # fmt: skip
            emit("delegate", {"task_id": child.id, "agent": item["agent"], "title": item["title"]})
            outcome = runner.run_task(conn, loaded, child.id, force=force)
            emit("result", {"task_id": child.id, "agent": item["agent"],
                            "status": outcome.task.status})  # fmt: skip
            outcomes.append(outcome)
        for problem in problems:
            emit("problem", {"detail": problem})
        turn.delegated += outcomes
        prompt = _results_prompt(
            outcomes, problems, final=round_no + 1 == MAX_ROUNDS,
            request=None if turn.session_id else request,
        )  # fmt: skip
    return _finish(conn, turn)  # pragma: no cover - loop always returns


def _finish(conn: sqlite3.Connection, turn: Turn, error: str | None = None) -> Turn:
    if error:
        turn.error = error
        repo.finish_task(conn, turn.task_id, "failed", error=error)
    else:
        repo.finish_task(conn, turn.task_id, "done", result=turn.reply)
    return turn


def turn_usage(conn: sqlite3.Connection, task_id: int) -> dict[str, float]:
    """Token/cost totals over the request's task and its delegated children."""
    row = conn.execute(
        "SELECT count(*) AS runs, coalesce(sum(tokens_in), 0) AS tokens_in,"
        " coalesce(sum(tokens_out), 0) AS tokens_out,"
        " coalesce(sum(cache_read_tokens), 0) AS cache_read,"
        " coalesce(sum(cache_write_tokens), 0) AS cache_write,"
        " coalesce(sum(cost_usd), 0) AS cost_usd FROM runs"
        " WHERE task_id IN (SELECT id FROM tasks WHERE id = ? OR parent_id = ?)",
        (task_id, task_id),
    ).fetchone()
    return dict(row)
