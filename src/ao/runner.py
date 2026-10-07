"""Task runner: executes queued tasks one at a time. No daemon, no polling.

A task is run when someone asks (`ao task run`, Jarvis delegating, the MCP server).
Status mapping for one attempt:
    ok      → done     (result stored on the task)
    refused → waiting  (over budget; retry later)
    error   → failed
    timeout → failed
Guards, all enforced in code: attempt limit (loop protection), and confidential
tasks never go to a non-local backend (minimal version of KAP-90).
"""

import sqlite3
from dataclasses import dataclass

from ao import run
from ao.agents import AgentError
from ao.backends.base import BackendError, Result
from ao.config import LoadedConfig
from ao.db import repo

MAX_ATTEMPTS = 3
RUNNABLE = ("queued", "waiting")


class TaskError(Exception):
    """The task cannot be run as requested."""


@dataclass(frozen=True)
class TaskOutcome:
    task: repo.Task
    run_id: int | None = None
    result: Result | None = None


def task_prompt(task: repo.Task) -> str:
    return f"{task.title}\n\n{task.body}" if task.body.strip() else task.title


def _fail(conn: sqlite3.Connection, task: repo.Task, status, error: str, kind: str):
    finished = repo.finish_task(conn, task.id, status, error=error)
    repo.log_event(conn, kind, agent=task.agent, task_id=task.id, data={"error": error})
    return TaskOutcome(task=finished)


def run_task(
    conn: sqlite3.Connection, loaded: LoadedConfig, task_id: int, *, force: bool = False
) -> TaskOutcome:
    task = repo.get_task(conn, task_id)
    if task.status not in RUNNABLE:
        raise TaskError(f"task {task.id} is {task.status}; only queued/waiting tasks run")
    if not task.agent:
        raise TaskError(f"task {task.id} has no agent; assign one with --agent")
    if task.attempts >= MAX_ATTEMPTS and not force:
        return _fail(
            conn, task, "failed",
            f"gave up after {task.attempts} attempts (loop guard); `ao task retry {task.id}`",
            "task.loop_guard",
        )  # fmt: skip

    try:
        prepared = run.prepare(
            loaded, task.agent, task_prompt(task), attachments=list(task.attachments)
        )
    except (AgentError, BackendError, run.RunError) as exc:
        return _fail(conn, task, "failed", str(exc), "task.failed")

    if task.classification == "confidential" and not prepared.backend.is_local:
        return _fail(
            conn, task, "failed",
            f"confidential task refused: backend {prepared.backend.name!r} is not local",
            "routing.refused",
        )  # fmt: skip

    repo.start_task(conn, task.id)
    record = run.execute(conn, prepared, task.id, force=force)
    result = record.result
    if result.outcome == "ok":
        final = repo.finish_task(conn, task.id, "done", result=result.text)
    elif result.outcome == "refused":
        final = repo.finish_task(conn, task.id, "waiting", error=result.error)
    else:
        final = repo.finish_task(conn, task.id, "failed", error=result.error)
    repo.log_event(conn, f"task.{final.status}", agent=task.agent, task_id=task.id,
                   run_id=record.run_id)  # fmt: skip
    return TaskOutcome(task=final, run_id=record.run_id, result=result)


def run_next(conn: sqlite3.Connection, loaded: LoadedConfig, *, force=False) -> TaskOutcome | None:
    task = repo.next_queued_task(conn)
    return run_task(conn, loaded, task.id, force=force) if task else None


def run_all(conn: sqlite3.Connection, loaded: LoadedConfig, *, force=False) -> list[TaskOutcome]:
    """Drain the queue in id order. Tasks that end up waiting/failed are not retried here."""
    outcomes = []
    while (outcome := run_next(conn, loaded, force=force)) is not None:
        outcomes.append(outcome)
    return outcomes
