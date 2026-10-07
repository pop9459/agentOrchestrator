"""`ao linear …`: mirror Linear (read-only), hand issues to agents."""

from contextlib import closing
from typing import Annotated

import typer

from ao import runner
from ao.cli_common import EXIT_RUN_FAILED, fail, load_or_exit, open_db
from ao.db import repo
from ao.linear import mirror
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
    typer.echo(f"#{task.id} {outcome.task.status}", err=True)
    if outcome.task.status == "done":
        typer.echo(outcome.task.result or "")
    else:
        typer.echo(outcome.task.error or "", err=True)
        raise typer.Exit(EXIT_RUN_FAILED)
