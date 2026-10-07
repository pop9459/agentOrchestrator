"""Change sets: the only path by which ao writes to Linear (KAP-95).

    propose_* → validated, allowlist-checked `pending` row (nothing is sent)
    apply     → re-checks the allowlist against *live* Linear data, then mutates
    reject    → never sent

Allowlist: `[linear] write_projects` (project names or ids). Creates must target an
allowlisted project; updates and comments must target an issue whose *current*
project is allowlisted.
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from ao.config import LoadedConfig
from ao.db import repo
from ao.linear import mirror
from ao.linear.client import ISSUE_FIELDS, LinearClient, LinearError

Kind = Literal["create_issue", "update_issue", "comment"]
UPDATE_FIELDS = ("title", "description", "state", "priority", "labels", "milestone")

CREATE_MUTATION = f"""
mutation Create($input: IssueCreateInput!) {{
  issueCreate(input: $input) {{ success issue {{ {ISSUE_FIELDS} }} }}
}}
"""
UPDATE_MUTATION = f"""
mutation Update($id: String!, $input: IssueUpdateInput!) {{
  issueUpdate(id: $id, input: $input) {{ success issue {{ {ISSUE_FIELDS} }} }}
}}
"""
COMMENT_MUTATION = """
mutation Comment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url } }
}
"""


class ChangeError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    id: int
    kind: Kind
    target_identifier: str | None
    project_id: str | None
    payload: dict[str, Any]
    summary: str
    status: str
    created_by: str
    task_id: int | None
    run_id: int | None
    created_at: str
    decided_at: str | None
    result: dict[str, Any] | None
    error: str | None


def _change(row: sqlite3.Row) -> Change:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    data["result"] = json.loads(data["result"]) if data["result"] else None
    return Change(**data)


# --- metadata lookups ---------------------------------------------------------------------


class Meta:
    """Name → id lookups from the mirror's metadata (case-insensitive names)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        team = mirror.get_meta(conn, "team")
        if not team:
            raise ChangeError("no Linear metadata yet; run `ao linear sync` first")
        self.team_id: str = team["id"]
        self.states = {s["name"].lower(): s for s in mirror.get_meta(conn, "states", [])}
        self.labels = {lbl["name"].lower(): lbl for lbl in mirror.get_meta(conn, "labels", [])}
        self.projects = mirror.get_meta(conn, "projects", [])

    def project(self, ref: str) -> dict[str, Any]:
        for p in self.projects:
            if ref in (p["id"], p["name"]) or ref.lower() == p["name"].lower():
                return p
        raise ChangeError(f"unknown project {ref!r}")

    def project_by_id(self, project_id: str | None) -> dict[str, Any] | None:
        return next((p for p in self.projects if p["id"] == project_id), None)

    def state(self, name: str) -> dict[str, Any]:
        try:
            return self.states[name.lower()]
        except KeyError:
            raise ChangeError(f"unknown state {name!r}; one of {sorted(self.states)}") from None

    def label(self, name: str) -> dict[str, Any]:
        try:
            return self.labels[name.lower()]
        except KeyError:
            raise ChangeError(f"unknown label {name!r}") from None

    def milestone(self, project: dict[str, Any], name: str) -> dict[str, Any]:
        for m in project.get("milestones", []):
            if m["name"].lower() == name.lower():
                return m
        raise ChangeError(f"unknown milestone {name!r} in project {project['name']!r}")


def allowed_project_ids(loaded: LoadedConfig, meta: Meta) -> set[str]:
    allowed = set()
    for ref in loaded.config.linear.write_projects:
        try:
            allowed.add(meta.project(ref)["id"])
        except ChangeError:
            continue  # configured project not (yet) in Linear: allows nothing
    return allowed


def _require_allowed(loaded: LoadedConfig, meta: Meta, project_id: str | None, what: str) -> None:
    if project_id is None or project_id not in allowed_project_ids(loaded, meta):
        project = meta.project_by_id(project_id)
        name = project["name"] if project else "no project"
        raise ChangeError(
            f"{what}: project {name!r} is not in [linear] write_projects "
            f"(allowed: {loaded.config.linear.write_projects or 'none'})"
        )


def _validate_fields(meta: Meta, project: dict[str, Any] | None, fields: dict[str, Any]) -> None:
    unknown = set(fields) - set(UPDATE_FIELDS)
    if unknown:
        raise ChangeError(f"unsupported fields: {sorted(unknown)}")
    if "state" in fields:
        meta.state(fields["state"])
    for name in fields.get("labels") or []:
        meta.label(name)
    if fields.get("milestone"):
        if project is None:
            raise ChangeError("a milestone needs a project")
        meta.milestone(project, fields["milestone"])
    if "priority" in fields and fields["priority"] not in (0, 1, 2, 3, 4):
        raise ChangeError("priority must be 0-4 (0 none, 1 urgent … 4 low)")
    if "title" in fields and not str(fields["title"]).strip():
        raise ChangeError("title must not be empty")


# --- propose -------------------------------------------------------------------------------


def _insert(
    conn: sqlite3.Connection,
    kind: Kind,
    target: str | None,
    project_id: str | None,
    payload: dict[str, Any],
    summary: str,
    created_by: str,
    task_id: int | None,
    run_id: int | None,
) -> Change:
    cur = conn.execute(
        "INSERT INTO linear_changes (kind, target_identifier, project_id, payload, summary,"
        " created_by, task_id, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, target, project_id, json.dumps(payload), summary, created_by, task_id, run_id),
    )
    repo.log_event(
        conn,
        "linear.proposed",
        agent=None if created_by == "user" else created_by,
        task_id=task_id,
        data={"change_id": cur.lastrowid, "kind": kind},
    )
    return get(conn, cur.lastrowid)


def propose_create(
    conn: sqlite3.Connection,
    loaded: LoadedConfig,
    *,
    project: str,
    title: str,
    description: str = "",
    labels: list[str] | None = None,
    milestone: str | None = None,
    priority: int | None = None,
    state: str | None = None,
    created_by: str = "user",
    task_id: int | None = None,
    run_id: int | None = None,
) -> Change:
    meta = Meta(conn)
    proj = meta.project(project)
    _require_allowed(loaded, meta, proj["id"], "create refused")
    fields = {
        "title": title,
        "description": description,
        "labels": labels or [],
        "milestone": milestone,
        "priority": priority,
        "state": state,
    }
    fields = {k: v for k, v in fields.items() if v not in (None, "", [])}
    _validate_fields(meta, proj, fields)
    return _insert(
        conn,
        "create_issue",
        None,
        proj["id"],
        fields,
        f"create issue in {proj['name']!r}: {title}",
        created_by,
        task_id,
        run_id,
    )


def propose_update(
    conn: sqlite3.Connection,
    loaded: LoadedConfig,
    identifier: str,
    fields: dict[str, Any],
    *,
    created_by: str = "user",
    task_id: int | None = None,
    run_id: int | None = None,
) -> Change:
    meta = Meta(conn)
    issue = mirror.get(conn, identifier)
    if issue is None:
        raise ChangeError(f"{identifier} not in the mirror (ao linear sync?)")
    _require_allowed(loaded, meta, issue.project_id, f"update of {issue.identifier} refused")
    if not fields:
        raise ChangeError("nothing to change")
    _validate_fields(meta, meta.project_by_id(issue.project_id), fields)
    keys = ", ".join(sorted(fields))
    return _insert(
        conn,
        "update_issue",
        issue.identifier,
        issue.project_id,
        fields,
        f"update {issue.identifier} ({keys})",
        created_by,
        task_id,
        run_id,
    )


def propose_comment(
    conn: sqlite3.Connection,
    loaded: LoadedConfig,
    identifier: str,
    body: str,
    *,
    created_by: str = "user",
    task_id: int | None = None,
    run_id: int | None = None,
) -> Change:
    meta = Meta(conn)
    issue = mirror.get(conn, identifier)
    if issue is None:
        raise ChangeError(f"{identifier} not in the mirror (ao linear sync?)")
    _require_allowed(loaded, meta, issue.project_id, f"comment on {issue.identifier} refused")
    if not body.strip():
        raise ChangeError("empty comment")
    return _insert(
        conn,
        "comment",
        issue.identifier,
        issue.project_id,
        {"body": body.strip()},
        f"comment on {issue.identifier}",
        created_by,
        task_id,
        run_id,
    )


# --- inspect ---------------------------------------------------------------------------------


def get(conn: sqlite3.Connection, change_id: int) -> Change:
    row = conn.execute("SELECT * FROM linear_changes WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        raise ChangeError(f"change {change_id} not found")
    return _change(row)


def list_changes(conn: sqlite3.Connection, status: str | None = None) -> list[Change]:
    if status:
        rows = conn.execute("SELECT * FROM linear_changes WHERE status = ? ORDER BY id", (status,))
    else:
        rows = conn.execute("SELECT * FROM linear_changes ORDER BY id")
    return [_change(r) for r in rows]


def render(conn: sqlite3.Connection, change: Change) -> str:
    lines = [f"change #{change.id} [{change.status}] {change.summary}  (by {change.created_by})"]
    if change.kind == "comment":
        lines += ["+ comment:", *(f"+   {line}" for line in change.payload["body"].splitlines())]
    elif change.kind == "create_issue":
        for key, value in change.payload.items():
            if key == "description":
                lines += ["+ description:", *(f"+   {line}" for line in str(value).splitlines())]
            else:
                lines.append(f"+ {key}: {value}")
    else:
        issue = mirror.get(conn, change.target_identifier or "")
        current = {
            "title": issue.title if issue else None,
            "description": issue.description if issue else None,
            "state": issue.state_name if issue else None,
            "priority": issue.priority if issue else None,
            "labels": list(issue.labels) if issue else None,
            "milestone": issue.milestone_name if issue else None,
        }
        for key, value in change.payload.items():
            lines += [f"- {key}: {current.get(key)}", f"+ {key}: {value}"]
    if change.result:
        lines.append(f"result: {change.result}")
    if change.error:
        lines.append(f"error: {change.error}")
    return "\n".join(lines)


# --- decide ------------------------------------------------------------------------------------


def _decide(conn: sqlite3.Connection, change_id: int, status: str, *, result=None, error=None):
    conn.execute(
        "UPDATE linear_changes SET status = ?, result = ?, error = ?,"
        " decided_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, error, change_id),
    )
    return get(conn, change_id)


def reject(conn: sqlite3.Connection, change_id: int) -> Change:
    change = get(conn, change_id)
    if change.status != "pending":
        raise ChangeError(f"change {change_id} is {change.status}")
    repo.log_event(conn, "linear.rejected", data={"change_id": change_id})
    return _decide(conn, change_id, "rejected")


def _input_fields(meta: Meta, project: dict[str, Any] | None, fields: dict[str, Any]):
    data: dict[str, Any] = {}
    if "title" in fields:
        data["title"] = fields["title"]
    if "description" in fields:
        data["description"] = fields["description"]
    if "state" in fields:
        data["stateId"] = meta.state(fields["state"])["id"]
    if "priority" in fields:
        data["priority"] = fields["priority"]
    if "labels" in fields:
        data["labelIds"] = [meta.label(name)["id"] for name in fields["labels"]]
    if fields.get("milestone") and project is not None:
        data["projectMilestoneId"] = meta.milestone(project, fields["milestone"])["id"]
    return data


def apply(
    conn: sqlite3.Connection, loaded: LoadedConfig, client: LinearClient, change_id: int
) -> Change:
    """Send one pending change to Linear after re-checking the allowlist on live data."""
    change = get(conn, change_id)
    if change.status != "pending":
        raise ChangeError(f"change {change_id} is {change.status}, not pending")
    meta = Meta(conn)
    try:
        if change.kind == "create_issue":
            _require_allowed(loaded, meta, change.project_id, "apply refused")
            project = meta.project_by_id(change.project_id)
            variables = {
                "input": {
                    "teamId": meta.team_id,
                    "projectId": change.project_id,
                    **_input_fields(meta, project, change.payload),
                }
            }
            data = client.query(CREATE_MUTATION, variables)["issueCreate"]
            issue = data.get("issue") if data.get("success") else None
            if issue is None:
                raise LinearError("issueCreate reported no success")
            mirror.upsert(conn, issue)
            result = {"identifier": issue["identifier"], "url": issue["url"]}
        else:
            live = client.issue(change.target_identifier or "")
            if live is None:
                raise ChangeError(f"{change.target_identifier} no longer exists in Linear")
            live_project = (live.get("project") or {}).get("id")
            _require_allowed(loaded, meta, live_project, "apply refused (issue moved?)")
            if change.kind == "update_issue":
                project = meta.project_by_id(live_project)
                variables = {
                    "id": live["id"],
                    "input": _input_fields(meta, project, change.payload),
                }
                data = client.query(UPDATE_MUTATION, variables)["issueUpdate"]
                if not data.get("success"):
                    raise LinearError("issueUpdate reported no success")
                mirror.upsert(conn, data["issue"])
                result = {"identifier": live["identifier"], "url": live["url"]}
            else:
                variables = {"input": {"issueId": live["id"], "body": change.payload["body"]}}
                data = client.query(COMMENT_MUTATION, variables)["commentCreate"]
                if not data.get("success"):
                    raise LinearError("commentCreate reported no success")
                result = {"identifier": live["identifier"], "url": data["comment"]["url"]}
    except (ChangeError, LinearError) as exc:
        repo.log_event(conn, "linear.failed", data={"change_id": change_id, "error": str(exc)})
        return _decide(conn, change_id, "failed", error=str(exc))
    repo.log_event(conn, "linear.applied", data={"change_id": change_id, **result})
    return _decide(conn, change_id, "applied", result=result)
