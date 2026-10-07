"""Board manager support (KAP-94): compact board metadata, drafting, review, memory seeding.

The board manager only ever *proposes*: drafts become pending change sets, review
suggestions become pending updates (with --propose). Nothing is written without
`ao linear apply`.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ao import memory, run
from ao.agents import load_agent
from ao.config import LoadedConfig
from ao.linear import changes, mirror

AGENT = "linear-manager"
MAX_BOARD_CHARS = 8_000  # ~2k tokens
ACTIVE_PROJECT_STATES = {None, "backlog", "planned", "started"}

HOUSE_RULES = [
    (
        "issue-format",
        "Issue descriptions: 1-2 lines of context, bullet details, then an "
        "'Acceptance' section with checkable criteria.",
    ),
    ("labels-existing", "Use only existing labels; never invent labels, projects or milestones."),
    (
        "one-outcome",
        "One issue = one deliverable; split bigger work into several issues under a milestone.",
    ),
    ("titles", "Titles are short and action-oriented (verb first), no trailing period."),
    (
        "writes-gated",
        "You only draft; changes reach Linear only after the user applies them (ao linear apply).",
    ),
]


def board_context(conn: sqlite3.Connection, write_projects: list[str] | None = None) -> str:
    """Compact metadata section for the board manager's system prompt."""
    states = mirror.get_meta(conn, "states", [])
    if not states:
        return ""
    lines = [
        "## Board (Linear team metadata)",
        "States: "
        + ", ".join(
            f"{s['name']} ({s['type']})" for s in sorted(states, key=lambda s: s.get("position", 0))
        ),
    ]
    lines.append("Labels:")
    for label in sorted(mirror.get_meta(conn, "labels", []), key=lambda lbl: lbl["name"]):
        desc = f": {label['description']}" if label.get("description") else ""
        lines.append(f"- {label['name']}{desc}")
    lines.append("Active projects (milestones):")
    for project in mirror.get_meta(conn, "projects", []):
        if project.get("state") not in ACTIVE_PROJECT_STATES:
            continue
        milestones = ", ".join(m["name"] for m in project.get("milestones", [])) or "-"
        lines.append(f"- {project['name']} ({milestones})")
    if write_projects is not None:
        lines.append(
            "Writable projects (new issues may only go here): "
            + (", ".join(write_projects) or "none")
        )
    text = "\n".join(lines)
    return text if len(text) <= MAX_BOARD_CHARS else text[:MAX_BOARD_CHARS] + "\n…(truncated)"


# --- draft -------------------------------------------------------------------------------------

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "project": {"type": "string"},
        "milestone": {"type": "string", "description": "empty string = none"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer", "description": "0 none, 1 urgent, 2 high, 3 medium, 4 low"},
        "reasoning": {"type": "string"},
    },
    "required": ["title", "description", "project", "milestone", "labels", "priority", "reasoning"],
}


@dataclass
class Draft:
    data: dict[str, Any]
    run_id: int
    change: changes.Change | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _board_extra(conn: sqlite3.Connection, loaded: LoadedConfig) -> list[tuple[str, str]]:
    return [("board", board_context(conn, loaded.config.linear.write_projects))]


def draft(
    conn: sqlite3.Connection, loaded: LoadedConfig, idea: str, project: str | None = None
) -> Draft:
    if not mirror.get_meta(conn, "team"):
        raise changes.ChangeError("no Linear metadata yet; run `ao linear sync` first")
    prompt = f"Draft one Linear issue for this idea:\n{idea.strip()}"
    if project:
        prompt += f"\n\nThe issue goes into project {project!r}."
    prepared = run.prepare(
        loaded, AGENT, prompt, extra_system=_board_extra(conn, loaded), output_schema=DRAFT_SCHEMA
    )
    record = run.execute(conn, prepared)
    if record.result.outcome != "ok" or record.result.structured is None:
        raise changes.ChangeError(f"draft run failed: {record.result.error}")
    data = dict(record.result.structured)
    result = Draft(data=data, run_id=record.run_id)
    meta = changes.Meta(conn)
    target = project or data.get("project", "")
    labels = []
    for name in data.get("labels") or []:
        try:
            labels.append(meta.label(name)["name"])
        except changes.ChangeError:
            result.warnings.append(f"dropped unknown label {name!r}")
    milestone = data.get("milestone") or None
    if milestone:
        try:
            meta.milestone(meta.project(target), milestone)
        except changes.ChangeError as exc:
            result.warnings.append(f"dropped milestone: {exc}")
            milestone = None
    priority = data.get("priority")
    try:
        result.change = changes.propose_create(
            conn,
            loaded,
            project=target,
            title=data.get("title", ""),
            description=data.get("description", ""),
            labels=labels,
            milestone=milestone,
            priority=priority if priority in (0, 1, 2, 3, 4) else None,
            created_by=AGENT,
            run_id=record.run_id,
        )
    except changes.ChangeError as exc:
        result.error = str(exc)
    return result


# --- review ------------------------------------------------------------------------------------

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "stale": {"type": "array", "items": {"type": "string"}},
        "missing_info": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"identifier": {"type": "string"}, "what": {"type": "string"}},
                "required": ["identifier", "what"],
            },
        },
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string"},
                    "field": {"type": "string", "enum": ["state", "priority", "labels", "title"]},
                    "value": {"type": "string", "description": "labels: comma-separated"},
                    "reason": {"type": "string"},
                },
                "required": ["identifier", "field", "value", "reason"],
            },
        },
    },
    "required": ["summary", "stale", "missing_info", "suggestions"],
}


@dataclass
class Review:
    data: dict[str, Any]
    run_id: int
    issue_count: int
    proposed: list[changes.Change] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _days_since(timestamp: str) -> int:
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return max(0, (datetime.now(UTC) - then).days)


def _description_status(description: str | None) -> str:
    text = (description or "").strip()
    if not text:
        return "NO description"
    has_acceptance = re.search(r"acceptance", text, re.IGNORECASE)
    return "description + acceptance" if has_acceptance else "description, no acceptance"


def issue_digest(issues: list[mirror.Issue]) -> str:
    """One line per issue; enough to judge staleness and completeness without bodies."""
    return "\n".join(
        f"{i.identifier} | {i.state_name} | {', '.join(i.labels) or 'no labels'} | "
        f"{_days_since(i.updated_at)}d since update | {_description_status(i.description)} | "
        f"{i.title}"
        for i in issues
    )


def _fields(suggestion: dict[str, Any]) -> dict[str, Any]:
    value = str(suggestion.get("value", "")).strip()
    match suggestion.get("field"):
        case "labels":
            return {"labels": [v.strip() for v in re.split(r",", value) if v.strip()]}
        case "priority":
            return {"priority": int(value)} if value.isdigit() else {}
        case "state" | "title" as name:
            return {name: value} if value else {}
    return {}


def review(
    conn: sqlite3.Connection,
    loaded: LoadedConfig,
    project: str | None = None,
    *,
    propose: bool = False,
    limit: int = 60,
) -> Review:
    issues = mirror.search(conn, project=project, open_only=True, limit=limit)
    if not issues:
        raise changes.ChangeError("no open mirrored issues to review (ao linear sync?)")
    prompt = (
        "Review these open issues. Flag stale ones and missing information, and suggest "
        f"concrete field changes.\n\n{issue_digest(issues)}"
    )
    prepared = run.prepare(
        loaded, AGENT, prompt, extra_system=_board_extra(conn, loaded), output_schema=REVIEW_SCHEMA
    )
    record = run.execute(conn, prepared)
    if record.result.outcome != "ok" or record.result.structured is None:
        raise changes.ChangeError(f"review run failed: {record.result.error}")
    result = Review(data=record.result.structured, run_id=record.run_id, issue_count=len(issues))
    if propose:
        for suggestion in result.data.get("suggestions", []):
            fields = _fields(suggestion)
            ident = suggestion.get("identifier", "?")
            if not fields:
                result.skipped.append(f"{ident}: unusable suggestion {suggestion}")
                continue
            try:
                result.proposed.append(
                    changes.propose_update(
                        conn, loaded, ident, fields, created_by=AGENT, run_id=record.run_id
                    )
                )
            except changes.ChangeError as exc:
                result.skipped.append(f"{ident}: {exc}")
    return result


# --- memory seeding ----------------------------------------------------------------------------


def seed_memory(conn: sqlite3.Connection, loaded: LoadedConfig) -> memory.Applied:
    """Deterministic convention facts (label descriptions + house rules). Idempotent."""
    agent = load_agent(loaded, AGENT)
    ops = [{"op": "add", "id": fact_id, "text": text} for fact_id, text in HOUSE_RULES]
    for label in mirror.get_meta(conn, "labels", []):
        if label.get("description"):
            ops.append(
                {
                    "op": "add",
                    "id": f"label-{memory.normalize_id(label['name'])}",
                    "text": f"Label {label['name']}: {label['description']}",
                }
            )
    outcome = memory.apply(memory.load(agent.dir), ops, agent.config.memory.max_facts)
    memory.save(agent.dir, outcome.facts)
    return outcome
