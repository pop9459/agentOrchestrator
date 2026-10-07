import json

import pytest
from typer.testing import CliRunner

from ao import backends
from ao.agents import create_agent
from ao.backends.base import BackendError, Result, Usage
from ao.cli import app
from ao.config import BackendConfig, load_config
from ao.db import repo, store
from ao.run import RunError, build_system_prompt, execute, prepare
from fakes import FakeBackend

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: backend)
    return backend


@pytest.fixture
def loaded(isolated_env):
    config = load_config()
    create_agent(config, "idea-ingestor")
    return config


def test_system_prompt_has_instructions_then_memory(loaded, fake):
    agent_dir = loaded.agents_dir / "idea-ingestor"
    (agent_dir / "memory").mkdir()
    (agent_dir / "memory" / "INDEX.md").write_text("- user likes bullet lists")
    prepared = prepare(loaded, "idea-ingestor", "hi")
    system = prepared.request.system_prompt
    assert system.startswith("You catalogue")
    assert system.endswith("## Memory\n- user likes bullet lists")
    assert build_system_prompt(prepared.agent) == system


def test_prepare_resolves_model_and_budget(loaded, fake):
    prepared = prepare(loaded, "idea-ingestor", "an idea")
    assert prepared.request.model == "haiku"
    assert prepared.backend is fake
    assert len(prepared.prompt_hash) == 64
    assert prepared.estimated_tokens > 0


def test_prepare_errors(loaded, fake):
    with pytest.raises(RunError, match="empty prompt"):
        prepare(loaded, "idea-ingestor", "   ")
    create_agent(loaded, "local")
    with pytest.raises(RunError, match="backend 'local' not configured"):
        prepare(loaded, "local", "x")


def test_execute_records_run_and_event(loaded, fake, tmp_path):
    conn = store.connect(tmp_path / "t.sqlite3")
    store.migrate(conn)
    prepared = prepare(loaded, "idea-ingestor", "an idea")
    record = execute(conn, prepared)

    assert record.result.text == "echo: an idea"
    assert prepared.agent.workspace.is_dir()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (record.run_id,)).fetchone()
    assert (row["agent"], row["backend"], row["model"], row["outcome"]) == (
        "idea-ingestor", "fake", "fake-model", "ok"
    )  # fmt: skip
    assert (row["tokens_in"], row["tokens_out"], row["cache_read_tokens"],
            row["cache_write_tokens"]) == (100, 10, 5, 20)  # fmt: skip
    assert row["prompt_hash"] == prepared.prompt_hash
    event = conn.execute("SELECT kind, run_id FROM events").fetchone()
    assert tuple(event) == ("run.finished", record.run_id)
    assert repo.usage_summary(conn, since="2000-01-01")[0].billable_tokens == 130


def test_registry_reports_planned_backends():
    with pytest.raises(BackendError, match="KAP-80"):
        backends.get_backend("local", BackendConfig(type="openai_compat"))


def test_cli_run_prints_text_and_footer(loaded, fake):
    result = runner.invoke(app, ["run", "idea-ingestor", "build a TUI later"])
    assert result.exit_code == 0, result.output
    assert "echo: build a TUI later" in result.stdout
    assert "[ok · fake-model · in 100 out 10" in result.stderr


def test_cli_run_json_and_stdin(loaded, fake):
    result = runner.invoke(app, ["run", "idea-ingestor", "--json"], input="from stdin")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["text"] == "echo: from stdin"
    assert payload["usage"]["cache_write_tokens"] == 20


def test_cli_run_failure_exit_code(loaded, fake):
    fake.results.append(Result(outcome="error", error="boom", usage=Usage()))
    result = runner.invoke(app, ["run", "idea-ingestor", "x"])
    assert result.exit_code == 1
    assert "error: boom" in result.stderr


def test_cli_dry_run_calls_nothing(loaded, fake):
    result = runner.invoke(app, ["run", "idea-ingestor", "x", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "fake-backend model=haiku" in result.stdout
    assert "--- system prompt ---" in result.stdout
    assert fake.requests == []


def test_cli_run_unknown_agent(loaded, fake):
    result = runner.invoke(app, ["run", "ghost", "x"])
    assert result.exit_code == 2
