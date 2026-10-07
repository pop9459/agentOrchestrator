"""Helpers shared by the CLI modules."""

import sqlite3
import sys

import typer

from ao.config import ConfigError, LoadedConfig, load_config
from ao.db import store

EXIT_RUN_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3


def load_or_exit() -> LoadedConfig:
    try:
        return load_config()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_USAGE) from exc


def open_db(loaded: LoadedConfig) -> sqlite3.Connection:
    """Open the state DB, creating/upgrading it as needed (migrations are cheap no-ops)."""
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    return conn


def fail(message: str, code: int = EXIT_USAGE) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


def text_or_stdin(value: str | None, what: str = "prompt") -> str:
    """`value`, or stdin when it is omitted or '-'."""
    if value is not None and value != "-":
        return value
    if sys.stdin.isatty():
        raise fail(f"no {what} given (pass it as an argument or on stdin)")
    return sys.stdin.read()
