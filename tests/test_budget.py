import json
from datetime import date

import pytest
from typer.testing import CliRunner

from ao import budget
from ao.agents import create_agent
from ao.cli import app
from ao.config import load_config
from ao.db import repo, store
from ao.run import execute, prepare
from fakes import FakeBackend

runner = CliRunner()


def write_toml(loaded, name, budget_section):
    path = loaded.agents_dir / name / "agent.toml"
    path.write_text(
        f'role = "budget test"\nbackend = "claude"\nmodel = "haiku"\n[budget]\n{budget_section}\n'
    )


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()  # each run: 100 in + 20 cache w + 10 out = 130 billable, $0.001
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: backend)
    return backend


@pytest.fixture
def setup(isolated_env, fake, tmp_path):
    loaded = load_config()
    create_agent(loaded, "scout", "blank")
    conn = store.connect(tmp_path / "b.sqlite3")
    store.migrate(conn)
    return loaded, conn


def test_parse_since():
    now = date(2026, 10, 7)
    assert budget.parse_since("today", now) == now
    assert budget.parse_since("1d", now) == now
    assert budget.parse_since("7d", now) == date(2026, 10, 1)
    assert budget.parse_since("2026-09-01", now) == date(2026, 9, 1)
    for bad in ("0d", "week", "2026-13-01"):
        with pytest.raises(ValueError):
            budget.parse_since(bad, now)


def test_daily_token_cap_refuses_next_run(setup, fake):
    loaded, conn = setup
    write_toml(loaded, "scout", "daily_tokens = 200")
    assert execute(conn, prepare(loaded, "scout", "1")).result.outcome == "ok"  # 130 used
    assert execute(conn, prepare(loaded, "scout", "2")).result.outcome == "ok"  # 260 used
    refused = execute(conn, prepare(loaded, "scout", "3"))

    assert refused.result.outcome == "refused"
    assert "260 of 200 daily tokens" in refused.result.error
    assert len(fake.requests) == 2  # backend never called for the refused run
    row = conn.execute("SELECT outcome, tokens_in FROM runs WHERE id = ?",
                       (refused.run_id,)).fetchone()  # fmt: skip
    assert tuple(row) == ("refused", 0)
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds[-1] == "run.refused"
    (usage,) = repo.usage_summary(conn, since="2000-01-01")
    assert usage.runs == 2  # refused runs are not usage


def test_force_overrides_and_is_logged(setup, fake):
    loaded, conn = setup
    write_toml(loaded, "scout", "daily_tokens = 1")
    execute(conn, prepare(loaded, "scout", "1"))
    forced = execute(conn, prepare(loaded, "scout", "2"), force=True)
    assert forced.result.outcome == "ok"
    override = conn.execute(
        "SELECT json_extract(data, '$.reason') FROM events WHERE kind = 'budget.override'"
    ).fetchone()[0]
    assert "daily tokens" in override


def test_daily_cost_cap(setup, fake):
    loaded, conn = setup
    write_toml(loaded, "scout", "daily_cost_usd = 0.0015")
    execute(conn, prepare(loaded, "scout", "1"))  # $0.001
    execute(conn, prepare(loaded, "scout", "2"))  # $0.002 total
    assert execute(conn, prepare(loaded, "scout", "3")).result.outcome == "refused"


def test_budgets_are_per_day(setup, fake):
    loaded, conn = setup
    write_toml(loaded, "scout", "daily_tokens = 100")
    repo.record_run(conn, agent="scout", backend="claude", outcome="ok", tokens_in=10_000,
                    started_at="2026-01-01T10:00:00.000Z")  # fmt: skip
    assert execute(conn, prepare(loaded, "scout", "x")).result.outcome == "ok"


def test_global_cap_only_counts_and_blocks_cloud_backends(isolated_env, fake, tmp_path):
    (isolated_env / "ao.toml").write_text(
        "[budgets]\nglobal_daily_tokens = 150\n"
        '[backends.local]\ntype = "openai_compat"\nis_local = true\n'
    )
    loaded = load_config()
    create_agent(loaded, "scout", "blank")
    conn = store.connect(tmp_path / "g.sqlite3")
    store.migrate(conn)
    repo.record_run(conn, agent="other", backend="local", outcome="ok", tokens_in=10_000)
    prepared = prepare(loaded, "scout", "x")
    assert execute(conn, prepared).result.outcome == "ok"  # local usage not counted
    assert execute(conn, prepared).result.outcome == "ok"  # 130 < 150
    assert execute(conn, prepared).result.outcome == "refused"  # 260 >= 150
    assert "global cloud budget" in execute(conn, prepared).result.error

    fake.local = True  # a local backend is never blocked by the cloud cap
    assert execute(conn, prepared).result.outcome == "ok"


def test_cli_refused_exit_code_and_force(isolated_env, fake, monkeypatch, tmp_path):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    create_agent(loaded, "scout", "blank")
    write_toml(loaded, "scout", "daily_tokens = 1")
    assert runner.invoke(app, ["run", "scout", "a"]).exit_code == 0
    refused = runner.invoke(app, ["run", "scout", "b"])
    assert refused.exit_code == 3
    assert "daily tokens" in refused.stderr
    assert runner.invoke(app, ["run", "scout", "c", "--force"]).exit_code == 0


def test_cli_usage_matches_runs(isolated_env, fake, monkeypatch, tmp_path):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    create_agent(loaded, "scout", "blank")
    for prompt in ("a", "b", "c"):
        runner.invoke(app, ["run", "scout", prompt])

    result = runner.invoke(app, ["usage", "--json", "--since", "today"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    (row,) = payload["rows"]
    assert (row["agent"], row["runs"], row["tokens_in"], row["tokens_out"]) == ("scout", 3, 300, 30)
    assert row["billable_tokens"] == 390
    assert payload["budgets_today"] == [{"scope": "scout tokens", "used": "390",
                                         "limit": "200,000"}]  # fmt: skip

    text = runner.invoke(app, ["usage"]).stdout
    assert "scout" in text and "billable tokens (in + cache w + out): 390" in text
    assert runner.invoke(app, ["usage", "--since", "nope"]).exit_code == 2
