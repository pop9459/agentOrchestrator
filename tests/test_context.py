import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ao import context
from ao.agents import create_agent, load_agent
from ao.cli import app
from ao.config import load_config
from ao.db import repo, store
from ao.run import RunError, prepare
from ao.runner import run_task
from fakes import FakeBackend

SNAPSHOTS = Path(__file__).parent / "snapshots"
cli = CliRunner()


def assert_snapshot(name: str, actual: str) -> None:
    """Compare with tests/snapshots/<name>; AO_UPDATE_SNAPSHOTS=1 rewrites it."""
    path = SNAPSHOTS / name
    if os.environ.get("AO_UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(actual)
    assert actual == path.read_text()


@pytest.fixture
def agent(isolated_env):
    loaded = load_config()
    directory = create_agent(loaded, "scout", "blank")
    (directory / "INSTRUCTIONS.md").write_text("You are Scout. Be terse.\n")
    (directory / "memory").mkdir()
    (directory / "memory" / "INDEX.md").write_text("- [style] prefers bullet lists\n")
    return load_agent(loaded, "scout")


@pytest.fixture
def doc(isolated_env):
    path = isolated_env / "notes.md"
    path.write_text("# Notes\nOffline mode matters.\n")
    return path


def render(ctx: context.Context) -> str:
    return f"{ctx.system_prompt}\n===== prompt =====\n{ctx.prompt}\n"


def test_snapshot_minimal(agent):
    agent = agent.__class__(**{**agent.__dict__, "memory_index": None})
    assert_snapshot("context_minimal.txt", render(context.build(agent, "Summarise this.")))


def test_snapshot_full_order(agent, doc):
    ctx = context.build(agent, "Summarise the notes.", [doc],
                        extra_system=[("roster", "## Agents\n- scout")])  # fmt: skip
    rendered = render(ctx).replace(str(doc), "<DOC>")
    assert_snapshot("context_full.txt", rendered)
    assert [s.name for s in ctx.sections] == [
        "instructions", "memory", "roster", "attachment:notes.md", "task"
    ]  # fmt: skip
    assert [s.part for s in ctx.sections] == ["system"] * 3 + ["prompt"] * 2


def test_empty_extra_sections_are_dropped(agent):
    ctx = context.build(agent, "x", extra_system=[("empty", "")])
    assert "empty" not in [s.name for s in ctx.sections]


def test_estimates_and_describe(agent, doc):
    ctx = context.build(agent, "Summarise.", [doc])
    assert ctx.est_tokens == context.estimate_tokens(ctx.system_prompt) + context.estimate_tokens(
        ctx.prompt
    )
    table = context.describe(ctx, limit=30_000)
    assert "attachment:notes.md" in table and "(limit 30,000)" in table


def test_attachment_errors(agent, isolated_env):
    with pytest.raises(context.ContextError, match="not found"):
        context.build(agent, "x", [isolated_env / "missing.md"])
    big = isolated_env / "big.txt"
    big.write_text("x" * (context.MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(context.ContextError, match="too large"):
        context.build(agent, "x", [big])
    binary = isolated_env / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x81")
    with pytest.raises(context.ContextError, match="UTF-8"):
        context.build(agent, "x", [binary])


def test_limit_refuses_oversized_context(agent, isolated_env):
    loaded = load_config()
    toml = loaded.agents_dir / "scout" / "agent.toml"
    toml.write_text('role = "r"\n[limits]\nmax_context_tokens = 100\n')
    with pytest.raises(RunError, match="over this agent's limit of 100"):
        prepare(loaded, "scout", "word " * 200)


def test_task_attachments_flow_into_the_prompt(agent, doc, monkeypatch, tmp_path):
    fake = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: fake)
    loaded = load_config()
    conn = store.connect(tmp_path / "c.sqlite3")
    store.migrate(conn)
    task = repo.add_task(conn, "Summarise", agent="scout", attachments=[str(doc)])
    assert run_task(conn, loaded, task.id).task.status == "done"
    assert "<document path=" in fake.requests[0].prompt
    assert "Offline mode matters." in fake.requests[0].prompt

    missing = repo.add_task(conn, "x", agent="scout", attachments=[str(tmp_path / "gone.md")])
    outcome = run_task(conn, loaded, missing.id)
    assert outcome.task.status == "failed" and "attachment not found" in outcome.task.error


def test_cli_dry_run_shows_section_table(agent, doc, monkeypatch):
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: FakeBackend())
    result = cli.invoke(app, ["run", "scout", "Summarise", "--attach", str(doc), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "# context (~tokens, chars/4):" in result.stdout
    assert "attachment:notes.md" in result.stdout
    assert "Offline mode matters." in result.stdout
