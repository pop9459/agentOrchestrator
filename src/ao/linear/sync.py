"""`ao linear sync`: pull the team's issues and metadata into the local mirror. Read-only."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ao.db import repo
from ao.linear import mirror
from ao.linear.client import LinearClient


@dataclass(frozen=True)
class SyncReport:
    team: str
    fetched: int
    deleted: int
    full: bool
    since: str | None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sync(
    conn: sqlite3.Connection, client: LinearClient, team_key: str, *, full: bool = False
) -> SyncReport:
    started = _now()
    team = client.team(team_key)
    previous = mirror.get_meta(conn, "last_sync")
    since = None if full or not previous or previous.get("team") != team_key else previous["at"]

    with repo.transaction(conn):
        mirror.set_meta(conn, "team", {"id": team["id"], "key": team["key"], "name": team["name"]})
        mirror.set_meta(conn, "states", team["states"]["nodes"])
        mirror.set_meta(conn, "labels", team["labels"])
        mirror.set_meta(conn, "projects", team["projects"])
        seen: set[str] = set()
        for node in client.issues(team["id"], updated_after=since):
            mirror.upsert(conn, node)
            seen.add(node["id"])
        deleted = mirror.delete_missing(conn, seen) if since is None else 0
        mirror.set_meta(conn, "last_sync", {"team": team_key, "at": started})
    report = SyncReport(team_key, len(seen), deleted, since is None, since)
    repo.log_event(
        conn,
        "linear.sync",
        data={"team": team_key, "fetched": report.fetched, "deleted": deleted, "full": report.full},
    )
    return report
