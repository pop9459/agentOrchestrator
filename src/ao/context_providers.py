"""Opt-in extra system sections per agent: `[context] providers = ["linear_board"]`.

Providers add small, stable sections (cache-friendly) so an agent has what it needs
however it is invoked: CLI, Jarvis delegation or MCP.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing

from ao.config import LoadedConfig
from ao.db import store


def linear_board(loaded: LoadedConfig) -> tuple[str, str]:
    from ao.linear.board import board_context  # lazy: avoids an import cycle via ao.run

    if not loaded.data.db_path.exists():
        return ("board", "")
    with closing(store.connect(loaded.data.db_path)) as conn:
        try:
            return ("board", board_context(conn, loaded.config.linear.write_projects))
        except sqlite3.OperationalError:  # DB not migrated yet
            return ("board", "")


PROVIDERS: dict[str, Callable[[LoadedConfig], tuple[str, str]]] = {"linear_board": linear_board}
