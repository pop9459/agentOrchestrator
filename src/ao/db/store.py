"""SQLite connection and versioned SQL migrations.

Migrations are `ao/db/migrations/NNNN_name.sql` files, applied in order, each in its
own transaction, and recorded in `schema_migrations`.
"""

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: Path) -> sqlite3.Connection:
    """Open the DB in autocommit mode; use `transaction()` for multi-statement writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def available_migrations() -> list[Migration]:
    found = []
    for entry in resources.files("ao.db.migrations").iterdir():
        if match := _MIGRATION_FILE.match(entry.name):
            found.append(Migration(int(match[1]), match[2], entry.read_text()))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return found


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    _ensure_migrations_table(conn)
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]


def migrate(conn: sqlite3.Connection) -> list[Migration]:
    """Apply pending migrations; returns the ones applied (empty when up to date)."""
    done = set(applied_versions(conn))
    applied = []
    for migration in available_migrations():
        if migration.version in done:
            continue
        # executescript commits any open transaction first, so BEGIN/COMMIT are explicit.
        # `name` is safe to inline: it matched _MIGRATION_FILE.
        script = (
            f"BEGIN;\n{migration.sql}\n"
            f"INSERT INTO schema_migrations (version, name) "
            f"VALUES ({migration.version}, '{migration.name}');\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(migration)
    return applied
