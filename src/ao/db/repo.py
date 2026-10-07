"""Thin data-access functions over the state store (no ORM)."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

TaskStatus = Literal["queued", "running", "waiting", "done", "failed", "canceled"]
Classification = Literal["public", "confidential"]
Outcome = Literal["ok", "error", "refused", "timeout"]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    body: str
    agent: str | None
    status: TaskStatus
    parent_id: int | None
    classification: Classification
    linear_issue_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UsageRow:
    agent: str
    backend: str
    model: str | None
    runs: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


def add_task(
    conn: sqlite3.Connection,
    title: str,
    body: str = "",
    *,
    agent: str | None = None,
    parent_id: int | None = None,
    classification: Classification = "public",
    linear_issue_id: str | None = None,
) -> Task:
    cur = conn.execute(
        "INSERT INTO tasks (title, body, agent, parent_id, classification, linear_issue_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (title, body, agent, parent_id, classification, linear_issue_id),
    )
    return get_task(conn, cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> Task:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"task {task_id} not found")
    return Task(**dict(row))


def list_tasks(conn: sqlite3.Connection, status: TaskStatus | None = None) -> list[Task]:
    if status is None:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id")
    else:
        rows = conn.execute("SELECT * FROM tasks WHERE status = ? ORDER BY id", (status,))
    return [Task(**dict(row)) for row in rows]


def update_task_status(conn: sqlite3.Connection, task_id: int, status: TaskStatus) -> Task:
    cur = conn.execute(
        "UPDATE tasks SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        " WHERE id = ?",
        (status, task_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"task {task_id} not found")
    return get_task(conn, task_id)


def record_run(
    conn: sqlite3.Connection,
    *,
    agent: str,
    backend: str,
    outcome: Outcome,
    task_id: int | None = None,
    model: str | None = None,
    prompt_hash: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO runs (task_id, agent, backend, model, prompt_hash, tokens_in, tokens_out,"
        " cost_usd, duration_ms, outcome, error, started_at, finished_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " coalesce(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), ?)",
        (
            task_id, agent, backend, model, prompt_hash, tokens_in, tokens_out,
            cost_usd, duration_ms, outcome, error, started_at, finished_at,
        ),
    )  # fmt: skip
    return cur.lastrowid


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    agent: str | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (kind, agent, task_id, run_id, data) VALUES (?, ?, ?, ?, ?)",
        (kind, agent, task_id, run_id, json.dumps(data) if data is not None else None),
    )
    return cur.lastrowid


def usage_summary(conn: sqlite3.Connection, since: str, agent: str | None = None) -> list[UsageRow]:
    """Totals per agent/backend/model for runs on or after `since` (ISO date `YYYY-MM-DD`)."""
    query = (
        "SELECT agent, backend, model, sum(runs) AS runs, sum(tokens_in) AS tokens_in,"
        " sum(tokens_out) AS tokens_out, sum(cost_usd) AS cost_usd"
        " FROM usage_daily WHERE day >= ?"
    )
    params: list[Any] = [since]
    if agent is not None:
        query += " AND agent = ?"
        params.append(agent)
    query += " GROUP BY agent, backend, model ORDER BY agent, backend, model"
    return [UsageRow(**dict(row)) for row in conn.execute(query, params)]
