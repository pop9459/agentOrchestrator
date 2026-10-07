"""`ao task …` commands."""

from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer

from ao import runner
from ao.cli_common import EXIT_RUN_FAILED, fail, load_or_exit, open_db, text_or_stdin
from ao.db import repo

task_app = typer.Typer(help="Queue and run tasks.", no_args_is_help=True)

STATUS_MARK = {"queued": "·", "running": "▶", "waiting": "⏸", "done": "✓", "failed": "✗",
               "canceled": "-"}  # fmt: skip


def _line(task: repo.Task, indent: int = 0) -> str:
    agent = task.agent or "unassigned"
    conf = " [confidential]" if task.classification == "confidential" else ""
    return f"{'  ' * indent}{STATUS_MARK[task.status]} #{task.id} {task.title}  ({agent}){conf}"


def _report(outcome: runner.TaskOutcome) -> None:
    task = outcome.task
    typer.echo(f"{STATUS_MARK[task.status]} #{task.id} {task.status}", err=True)
    if task.status == "done" and task.result:
        typer.echo(task.result)
    elif task.error:
        typer.echo(f"  {task.error}", err=True)


@task_app.command("add")
def task_add(
    title: Annotated[str, typer.Argument(help="Short title; also the prompt if no body.")],
    body: Annotated[
        str | None, typer.Option("--body", help="Details for the agent; '-' reads stdin.")
    ] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent to run it.")] = None,
    confidential: Annotated[
        bool, typer.Option("--confidential", help="Only local backends may run it.")
    ] = False,
    parent: Annotated[int | None, typer.Option("--parent", help="Parent task id.")] = None,
    attach: Annotated[
        list[str] | None, typer.Option("--attach", help="File to include (repeatable).")
    ] = None,
) -> None:
    """Queue a task."""
    loaded = load_or_exit()
    body_text = text_or_stdin(body, "body") if body is not None else ""
    with closing(open_db(loaded)) as conn:
        depth = 0
        if parent is not None:
            try:
                depth = repo.get_task(conn, parent).depth + 1
            except KeyError as exc:
                raise fail(str(exc)) from exc
        task = repo.add_task(
            conn, title, body_text, agent=agent, parent_id=parent, depth=depth,
            classification="confidential" if confidential else "public",
            attachments=[str(Path(p).resolve()) for p in attach or []],
        )  # fmt: skip
        repo.log_event(conn, "task.queued", agent=agent, task_id=task.id)
    typer.echo(f"queued #{task.id}")


@task_app.command("list")
def task_list(
    status: Annotated[str | None, typer.Option("--status", help="Filter by status.")] = None,
    tree: Annotated[bool, typer.Option("--tree", help="Show parent/child structure.")] = False,
) -> None:
    """List tasks."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        if status is not None and status not in STATUS_MARK:
            raise fail(f"unknown status {status!r}; one of {', '.join(STATUS_MARK)}")
        tasks = repo.list_tasks(conn, status=status)  # type: ignore[arg-type]
        if not tasks:
            typer.echo("no tasks")
            return
        if not tree:
            for task in tasks:
                typer.echo(_line(task))
            return
        ids = {t.id for t in tasks}
        children: dict[int | None, list[repo.Task]] = {}
        for task in tasks:
            key = task.parent_id if task.parent_id in ids else None
            children.setdefault(key, []).append(task)

        def walk(parent_id: int | None, indent: int) -> None:
            for task in children.get(parent_id, []):
                typer.echo(_line(task, indent))
                walk(task.id, indent + 1)

        walk(None, 0)


@task_app.command("show")
def task_show(task_id: int) -> None:
    """Show a task with its runs and result."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            task = repo.get_task(conn, task_id)
        except KeyError as exc:
            raise fail(str(exc)) from exc
        typer.echo(_line(task))
        typer.echo(f"status: {task.status} · attempts: {task.attempts} · depth: {task.depth}")
        if task.parent_id:
            typer.echo(f"parent: #{task.parent_id}")
        if task.attachments:
            typer.echo(f"attachments: {', '.join(task.attachments)}")
        if task.body:
            typer.echo(f"--- body ---\n{task.body}")
        for row in repo.runs_for_task(conn, task.id):
            typer.echo(
                f"run #{row['id']}: {row['outcome']} · {row['model'] or '?'}"
                f" · in {row['tokens_in']} out {row['tokens_out']}"
                f" cache r{row['cache_read_tokens']} w{row['cache_write_tokens']}"
                f" · ${row['cost_usd'] or 0:.4f}"
            )
        for child in repo.child_tasks(conn, task.id):
            typer.echo(f"child: {_line(child)}")
        if task.error:
            typer.echo(f"--- error ---\n{task.error}")
        if task.result:
            typer.echo(f"--- result ---\n{task.result}")


@task_app.command("run")
def task_run(
    task_id: Annotated[int | None, typer.Argument(help="Task id.")] = None,
    next_: Annotated[bool, typer.Option("--next", help="Run the oldest queued task.")] = False,
    all_: Annotated[bool, typer.Option("--all", help="Drain the whole queue.")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Ignore budget and attempt limits (logged).")
    ] = False,
) -> None:
    """Run a task (or the queue). Exit code 1 if any task did not finish."""
    if sum([task_id is not None, next_, all_]) != 1:
        raise fail("give exactly one of: TASK_ID, --next, --all")
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            if all_:
                outcomes = runner.run_all(conn, loaded, force=force)
            elif next_:
                outcomes = [o] if (o := runner.run_next(conn, loaded, force=force)) else []
            else:
                outcomes = [runner.run_task(conn, loaded, task_id, force=force)]
        except (KeyError, runner.TaskError) as exc:
            raise fail(str(exc)) from exc
    if not outcomes:
        typer.echo("queue empty")
    for outcome in outcomes:
        _report(outcome)
    if any(o.task.status != "done" for o in outcomes):
        raise typer.Exit(EXIT_RUN_FAILED)


@task_app.command("retry")
def task_retry(task_id: int) -> None:
    """Re-queue a failed/waiting task and reset its attempt counter."""
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        try:
            task = repo.get_task(conn, task_id)
            if task.status in ("running", "done"):
                raise fail(f"task {task_id} is {task.status}")
            repo.reset_task(conn, task_id)
        except KeyError as exc:
            raise fail(str(exc)) from exc
        repo.log_event(conn, "task.retry", agent=task.agent, task_id=task_id)
    typer.echo(f"re-queued #{task_id}")
