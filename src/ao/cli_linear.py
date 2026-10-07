"""`ao linear …`: mirror Linear (read-only), hand issues to agents."""

from contextlib import closing
from typing import Annotated

import typer

from ao import runner
from ao.cli_common import EXIT_RUN_FAILED, fail, load_or_exit, open_db
from ao.db import repo
from ao.linear import changes, mirror
from ao.linear.client import LinearError, client_from_config
from ao.linear.sync import sync

linear_app = typer.Typer(
    help="Linear: sync the board, hand issues to agents.", no_args_is_help=True
)

PRIORITY = {0: "-", 1: "urgent", 2: "high", 3: "medium", 4: "low"}


def _row(issue: mirror.Issue) -> str:
    project = f" · {issue.project_name}" if issue.project_name else ""
    return f"{issue.identifier:<8} [{issue.state_name or '?'}] {issue.title}{project}"


@linear_app.command("sync")
def linear_sync(
    full: Annotated[
        bool, typer.Option("--full", help="Re-fetch everything; drop deleted.")
    ] = False,
) -> None:
    """Pull the team's issues and metadata into the local mirror (read-only)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            report = sync(conn, client_from_config(loaded), loaded.config.linear.team, full=full)
        except LinearError as exc:
            raise fail(str(exc)) from exc
    mode = "full" if report.full else f"since {report.since}"
    typer.echo(
        f"synced {report.team} ({mode}): {report.fetched} issues"
        + (f", {report.deleted} removed" if report.deleted else "")
    )


@linear_app.command("issues")
def linear_issues(
    project: Annotated[str | None, typer.Option("--project", help="Project name.")] = None,
    state: Annotated[
        str | None, typer.Option("--state", help="State type: backlog, unstarted, started, …")
    ] = None,
    search: Annotated[str | None, typer.Option("--search", help="Text in title/body.")] = None,
    open_only: Annotated[bool, typer.Option("--open", help="Hide done/canceled.")] = False,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """List mirrored issues (run `ao linear sync` first)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        issues = mirror.search(
            conn, project=project, state_type=state, text=search, open_only=open_only, limit=limit
        )
        last = mirror.get_meta(conn, "last_sync")
    if not issues:
        typer.echo("no matching issues" + ("" if last else " (never synced: ao linear sync)"))
    for issue in issues:
        typer.echo(_row(issue))


@linear_app.command("show")
def linear_show(identifier: str) -> None:
    """Show one mirrored issue."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        issue = mirror.get(conn, identifier)
    if issue is None:
        raise fail(f"{identifier} not in the mirror (ao linear sync?)")
    typer.echo(_row(issue))
    labels = ", ".join(issue.labels) or "-"
    priority = PRIORITY.get(issue.priority or 0, "?")
    typer.echo(
        f"priority: {priority} · labels: {labels} · milestone: {issue.milestone_name or '-'}"
    )
    typer.echo(f"assignee: {issue.assignee or '-'} · updated: {issue.updated_at} · {issue.url}")
    if issue.description:
        typer.echo(f"--- description ---\n{issue.description}")


@linear_app.command("task")
def linear_task(
    identifier: str,
    agent: Annotated[str, typer.Option("--agent", help="Agent to hand the issue to.")],
    run: Annotated[bool, typer.Option("--run", help="Run the task immediately.")] = False,
    comment_back: Annotated[
        bool, typer.Option("--comment-back", help="Propose a result comment on the issue.")
    ] = False,
) -> None:
    """Hand a mirrored issue to an agent as an ao task (linked by identifier)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        issue = mirror.get(conn, identifier)
        if issue is None:
            raise fail(f"{identifier} not in the mirror (ao linear sync?)")
        task = repo.add_task(
            conn,
            f"{issue.identifier}: {issue.title}",
            issue.description or "",
            agent=agent,
            linear_issue_id=issue.identifier,
        )
        repo.log_event(
            conn, "task.queued", agent=agent, task_id=task.id, data={"linear": issue.identifier}
        )
        typer.echo(f"queued #{task.id} for {agent} ({issue.identifier})")
        if not run:
            return
        outcome = runner.run_task(conn, loaded, task.id)
        if comment_back and outcome.task.status == "done":
            body = f"ao task #{task.id} ({agent}) finished:\n\n{(outcome.task.result or '')[:1500]}"
            try:
                change = changes.propose_comment(
                    conn,
                    loaded,
                    issue.identifier,
                    body,
                    created_by=agent,
                    task_id=task.id,
                    run_id=outcome.run_id,
                )
                typer.echo(
                    f"proposed comment as change #{change.id} (ao linear apply {change.id})",
                    err=True,
                )
            except changes.ChangeError as exc:
                typer.echo(f"comment-back not proposed: {exc}", err=True)
    typer.echo(f"#{task.id} {outcome.task.status}", err=True)
    if outcome.task.status == "done":
        typer.echo(outcome.task.result or "")
    else:
        typer.echo(outcome.task.error or "", err=True)
        raise typer.Exit(EXIT_RUN_FAILED)


# --- gated writes (KAP-95): propose → review → apply ---------------------------------------


def _proposed(change: changes.Change) -> None:
    typer.echo(f"proposed change #{change.id}: {change.summary}")
    typer.echo(f"review with `ao linear change {change.id}`, then `ao linear apply {change.id}`")


@linear_app.command("new")
def linear_new(
    project: Annotated[str, typer.Option("--project", help="Target project (allowlisted).")],
    title: Annotated[str, typer.Option("--title")],
    description: Annotated[str, typer.Option("--description")] = "",
    label: Annotated[list[str] | None, typer.Option("--label", help="Repeatable.")] = None,
    milestone: Annotated[str | None, typer.Option("--milestone")] = None,
    priority: Annotated[int | None, typer.Option("--priority", help="0-4")] = None,
) -> None:
    """Propose a new issue (nothing is sent until `apply`)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            change = changes.propose_create(
                conn,
                loaded,
                project=project,
                title=title,
                description=description,
                labels=label or [],
                milestone=milestone,
                priority=priority,
            )
        except changes.ChangeError as exc:
            raise fail(str(exc)) from exc
    _proposed(change)


@linear_app.command("edit")
def linear_edit(
    identifier: str,
    title: Annotated[str | None, typer.Option("--title")] = None,
    state: Annotated[
        str | None, typer.Option("--state", help="State name, e.g. 'In Progress'")
    ] = None,
    priority: Annotated[int | None, typer.Option("--priority", help="0-4")] = None,
    label: Annotated[
        list[str] | None, typer.Option("--label", help="Replaces all labels; repeatable.")
    ] = None,
) -> None:
    """Propose an update to an issue (nothing is sent until `apply`)."""
    fields = {"title": title, "state": state, "priority": priority, "labels": label}
    fields = {k: v for k, v in fields.items() if v is not None}
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            change = changes.propose_update(conn, loaded, identifier, fields)
        except changes.ChangeError as exc:
            raise fail(str(exc)) from exc
    _proposed(change)


@linear_app.command("comment")
def linear_comment(identifier: str, body: str) -> None:
    """Propose a comment (nothing is sent until `apply`)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            change = changes.propose_comment(conn, loaded, identifier, body)
        except changes.ChangeError as exc:
            raise fail(str(exc)) from exc
    _proposed(change)


@linear_app.command("pending")
def linear_pending(
    all_: Annotated[bool, typer.Option("--all", help="Include decided changes.")] = False,
) -> None:
    """List proposed changes waiting for a decision."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        rows = changes.list_changes(conn, None if all_ else "pending")
    if not rows:
        typer.echo("no pending changes")
    for change in rows:
        typer.echo(f"#{change.id} [{change.status}] {change.summary}  (by {change.created_by})")


@linear_app.command("change")
def linear_change(change_id: int) -> None:
    """Show a change set as a diff."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            typer.echo(changes.render(conn, changes.get(conn, change_id)))
        except changes.ChangeError as exc:
            raise fail(str(exc)) from exc


@linear_app.command("apply")
def linear_apply(
    change_id: int,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without asking.")] = False,
) -> None:
    """Send one pending change to Linear (re-checks the allowlist on live data)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            change = changes.get(conn, change_id)
            typer.echo(changes.render(conn, change))
            if change.status != "pending":
                raise fail(f"change {change_id} is {change.status}")
            if not yes and not typer.confirm("send this to Linear?", default=False):
                typer.echo("not applied")
                return
            done = changes.apply(conn, loaded, client_from_config(loaded), change_id)
        except (changes.ChangeError, LinearError) as exc:
            raise fail(str(exc)) from exc
    if done.status != "applied":
        raise fail(f"change #{change_id} failed: {done.error}", EXIT_RUN_FAILED)
    typer.echo(f"applied #{change_id}: {done.result}")


@linear_app.command("reject")
def linear_reject(change_id: int) -> None:
    """Discard a pending change (it is never sent)."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            changes.reject(conn, change_id)
        except changes.ChangeError as exc:
            raise fail(str(exc)) from exc
    typer.echo(f"rejected #{change_id}")
