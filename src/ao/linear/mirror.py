"""Local mirror of Linear issues and team metadata (tables from migration 0005)."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    title: str
    description: str | None
    state_name: str | None
    state_type: str | None
    priority: int | None
    labels: tuple[str, ...]
    project_id: str | None
    project_name: str | None
    milestone_name: str | None
    assignee: str | None
    parent_identifier: str | None
    url: str | None
    updated_at: str
    synced_at: str


def _issue(row: sqlite3.Row) -> Issue:
    data = dict(row)
    data["labels"] = tuple(json.loads(data["labels"]))
    return Issue(**data)


def flatten(node: dict[str, Any]) -> dict[str, Any]:
    """GraphQL issue node → mirror row."""

    def name(obj: dict[str, Any] | None, field: str = "name") -> Any:
        return obj.get(field) if obj else None

    return {
        "id": node["id"],
        "identifier": node["identifier"],
        "title": node["title"],
        "description": node.get("description"),
        "state_name": name(node.get("state")),
        "state_type": name(node.get("state"), "type"),
        "priority": node.get("priority"),
        "labels": json.dumps([lbl["name"] for lbl in (node.get("labels") or {}).get("nodes", [])]),
        "project_id": name(node.get("project"), "id"),
        "project_name": name(node.get("project")),
        "milestone_name": name(node.get("projectMilestone")),
        "assignee": name(node.get("assignee")),
        "parent_identifier": name(node.get("parent"), "identifier"),
        "url": node.get("url"),
        "updated_at": node["updatedAt"],
    }


def upsert(conn: sqlite3.Connection, node: dict[str, Any]) -> None:
    row = flatten(node)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{c}" for c in row)
    updates = ", ".join(f"{c} = excluded.{c}" for c in row if c != "id")
    conn.execute(
        f"INSERT INTO linear_issues ({columns}) VALUES ({placeholders})"
        f" ON CONFLICT(id) DO UPDATE SET {updates},"
        " synced_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        row,
    )


def delete_missing(conn: sqlite3.Connection, keep_ids: set[str]) -> int:
    existing = {r[0] for r in conn.execute("SELECT id FROM linear_issues")}
    gone = existing - keep_ids
    conn.executemany("DELETE FROM linear_issues WHERE id = ?", [(i,) for i in gone])
    return len(gone)


def get(conn: sqlite3.Connection, identifier: str) -> Issue | None:
    row = conn.execute(
        "SELECT * FROM linear_issues WHERE identifier = ?", (identifier.upper(),)
    ).fetchone()
    return _issue(row) if row else None


def search(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    state_type: str | None = None,
    text: str | None = None,
    open_only: bool = False,
    limit: int = 50,
) -> list[Issue]:
    clauses, params = [], []
    if project:
        clauses.append("lower(project_name) = lower(?)")
        params.append(project)
    if state_type:
        clauses.append("state_type = ?")
        params.append(state_type)
    if open_only:
        clauses.append("coalesce(state_type, '') NOT IN ('completed', 'canceled')")
    if text:
        clauses.append("(title LIKE ? OR description LIKE ? OR identifier LIKE ?)")
        params += [f"%{text}%"] * 3
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM linear_issues {where} ORDER BY updated_at DESC LIMIT ?", (*params, limit)
    )
    return [_issue(r) for r in rows]


def set_meta(conn: sqlite3.Connection, key: str, data: Any) -> None:
    conn.execute(
        "INSERT INTO linear_meta (key, data) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET data = excluded.data",
        (key, json.dumps(data)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT data FROM linear_meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default
