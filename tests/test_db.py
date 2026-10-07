import sqlite3

import pytest
from typer.testing import CliRunner

from ao.cli import app
from ao.db import repo, store

runner = CliRunner()


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "db" / "test.sqlite3")
    store.migrate(connection)
    yield connection
    connection.close()


def test_migrate_is_idempotent(tmp_path):
    conn = store.connect(tmp_path / "ao.sqlite3")
    first = store.migrate(conn)
    assert [m.version for m in first] == [m.version for m in store.available_migrations()]
    assert store.migrate(conn) == []
    assert store.applied_versions(conn) == [m.version for m in first]


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "ao.sqlite3")
    broken = store.Migration(1, "broken", "CREATE TABLE ok (id INTEGER);\nNOT VALID SQL;")
    monkeypatch.setattr(store, "available_migrations", lambda: [broken])
    with pytest.raises(sqlite3.Error):
        store.migrate(conn)
    assert store.applied_versions(conn) == []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" not in tables


def test_task_lifecycle(conn):
    parent = repo.add_task(conn, "Plan week", agent="jarvis")
    child = repo.add_task(
        conn, "Summarise mail", agent="local", parent_id=parent.id, classification="confidential"
    )
    assert child.status == "queued"
    assert child.classification == "confidential"
    assert repo.update_task_status(conn, child.id, "done").status == "done"
    assert [t.id for t in repo.list_tasks(conn, status="queued")] == [parent.id]
    assert len(repo.list_tasks(conn)) == 2


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("INSERT INTO tasks (title, status) VALUES (?, ?)", ("t", "bogus")),
        ("INSERT INTO tasks (title, classification) VALUES (?, ?)", ("t", "secret")),
        ("INSERT INTO runs (agent, backend, outcome) VALUES (?, ?, ?)", ("a", "b", "maybe")),
        (
            "INSERT INTO runs (agent, backend, outcome, tokens_in) VALUES (?,?,?,?)",
            ("a", "b", "ok", -1),
        ),
        ("INSERT INTO events (kind, data) VALUES (?, ?)", ("k", "{not json")),
    ],
)
def test_check_constraints(conn, sql, params):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_task(conn, "orphan", parent_id=999)


def test_missing_task_raises(conn):
    with pytest.raises(KeyError):
        repo.get_task(conn, 42)
    with pytest.raises(KeyError):
        repo.update_task_status(conn, 42, "done")


def test_usage_summary_aggregates_runs(conn):
    task = repo.add_task(conn, "t")
    for tokens_in, tokens_out, day in [(100, 10, "2026-10-06"), (50, 5, "2026-10-07")]:
        repo.record_run(
            conn, agent="jarvis", backend="claude", model="haiku", outcome="ok",
            task_id=task.id, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=0.01,
            started_at=f"{day}T10:00:00.000Z",
        )  # fmt: skip
    repo.record_run(conn, agent="local", backend="llama", outcome="error", tokens_in=7,
                    started_at="2026-10-07T11:00:00.000Z")  # fmt: skip

    rows = repo.usage_summary(conn, since="2026-10-06")
    by_agent = {r.agent: r for r in rows}
    assert by_agent["jarvis"].runs == 2
    assert by_agent["jarvis"].tokens_in == 150
    assert by_agent["jarvis"].tokens_out == 15
    assert by_agent["jarvis"].cost_usd == pytest.approx(0.02)
    assert by_agent["local"].cost_usd == 0

    assert [r.agent for r in repo.usage_summary(conn, since="2026-10-07", agent="jarvis")] == [
        "jarvis"
    ]
    assert repo.usage_summary(conn, since="2026-10-07", agent="jarvis")[0].tokens_in == 50


def test_log_event_stores_json(conn):
    event_id = repo.log_event(conn, "routing.refused", agent="jarvis", data={"reason": "conf"})
    row = conn.execute("SELECT kind, json_extract(data, '$.reason') FROM events WHERE id = ?",
                       (event_id,)).fetchone()  # fmt: skip
    assert tuple(row) == ("routing.refused", "conf")


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError), repo.transaction(conn):
        repo.add_task(conn, "temp")
        raise RuntimeError("boom")
    assert repo.list_tasks(conn) == []


def test_cli_db_migrate_and_status(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 1
    assert "ao db migrate" in result.output

    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0, result.output
    assert "applied 0001_init" in result.output
    assert "up to date" in runner.invoke(app, ["db", "migrate"]).output

    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 0
    assert "applied:  0001" in result.output
    assert "tasks:    0" in result.output
