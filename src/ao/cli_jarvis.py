"""`ao ask` and `ao chat`: talk to J.A.R.V.I.S."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any

import typer

from ao import jarvis
from ao.cli_common import EXIT_RUN_FAILED, load_or_exit, open_db, text_or_stdin
from ao.config import LoadedConfig


def _progress(kind: str, data: dict[str, Any]) -> None:
    if kind == "delegate":
        typer.echo(f"  → #{data['task_id']} {data['agent']}: {data['title']}", err=True)
    elif kind == "result":
        typer.echo(f"  {'✓' if data['status'] == 'done' else '✗'} #{data['task_id']} "
                   f"{data['agent']} {data['status']}", err=True)  # fmt: skip
    elif kind == "problem":
        typer.echo(f"  ! {data['detail']}", err=True)


def _footer(conn: sqlite3.Connection, turn: jarvis.Turn) -> str:
    u = jarvis.turn_usage(conn, turn.task_id)
    tokens = (
        f"in {u['tokens_in']} out {u['tokens_out']} cache r{u['cache_read']} w{u['cache_write']}"
    )
    return f"[{u['runs']} runs · {tokens} · ${u['cost_usd']:.4f} · task #{turn.task_id}]"


def _show(conn: sqlite3.Connection, turn: jarvis.Turn) -> None:
    if turn.error:
        typer.echo(f"error: {turn.error}", err=True)
    else:
        typer.echo(turn.reply)
    typer.echo(_footer(conn, turn), err=True)


def ask(
    request: Annotated[str | None, typer.Argument(help="Your request; stdin if omitted.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignore budgets (logged).")] = False,
) -> None:
    """One request to Jarvis (fresh context; delegations run automatically)."""
    text = text_or_stdin(request, "request")
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        turn = jarvis.handle(conn, loaded, text, force=force, on_event=_progress)
        _show(conn, turn)
    if turn.error:
        raise typer.Exit(EXIT_RUN_FAILED)


def _session_file(loaded: LoadedConfig) -> Path:
    return loaded.data.root / "chat" / "jarvis.session"


def chat(
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue the last chat session.")
    ] = False,
) -> None:
    """Interactive chat with Jarvis. One Claude session carries the conversation.

    Commands: /new (fresh session), /usage (this session's totals), /quit.
    """
    loaded = load_or_exit()
    session_file = _session_file(loaded)
    session_id = None
    if resume and session_file.is_file():
        session_id = session_file.read_text().strip() or None
    typer.echo(f"J.A.R.V.I.S chat ({'resumed ' + session_id[:8] if session_id else 'new session'}"
               "). /new /usage /quit", err=True)  # fmt: skip
    turns: list[int] = []
    with closing(open_db(loaded)) as conn:
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                typer.echo("", err=True)
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/new":
                session_id, turns = None, []
                typer.echo("(new session)", err=True)
                continue
            if line == "/usage":
                totals = [jarvis.turn_usage(conn, t) for t in turns]
                cost = sum(t["cost_usd"] for t in totals)
                tokens = sum(t["tokens_in"] + t["tokens_out"] + t["cache_write"] for t in totals)
                typer.echo(f"{len(turns)} turns · {tokens} billable tokens · ${cost:.4f}",
                           err=True)  # fmt: skip
                continue
            turn = jarvis.handle(conn, loaded, line, session_id=session_id, persist_session=True,
                                 on_event=_progress)  # fmt: skip
            _show(conn, turn)
            turns.append(turn.task_id)
            if turn.session_id:
                session_id = turn.session_id
                session_file.parent.mkdir(parents=True, exist_ok=True)
                session_file.write_text(session_id)
