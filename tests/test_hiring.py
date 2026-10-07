import pytest
from typer.testing import CliRunner

from ao import hiring
from ao.agents import create_agent, list_agent_names, load_agent
from ao.backends.base import Result, Usage
from ao.cli import app
from ao.config import load_config
from ao.db import store
from fakes import FakeBackend

cli = CliRunner()


def proposal(**overrides):
    data = {
        "name": "note-summariser", "role": "Summarises lecture notes", "backend": "claude",
        "model": "haiku", "clearance": "public", "prompt_mode": "replace", "tools": [],
        "max_turns": 3, "daily_tokens": 80000,
        "instructions": "You summarise lecture notes into 5 bullets.", "reasoning": "Cheap task.",
    }  # fmt: skip
    data.update(overrides)
    return Result(outcome="ok", text="{}", structured=data, model="fake",
                  usage=Usage(input_tokens=30, output_tokens=40))  # fmt: skip


@pytest.fixture
def env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    fake = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: fake)
    return loaded, conn, fake


def test_catalogue_lists_agents_and_backends(env):
    loaded, _, _ = env
    create_agent(loaded, "jarvis")
    text = hiring.catalogue(loaded)
    assert "- jarvis: Top-level orchestrator" in text
    assert "- claude: claude_code (cloud); models: haiku | sonnet | opus" in text
    assert "no local backend configured yet" in text


def test_propose_and_write_roundtrip(env):
    loaded, conn, fake = env
    hiring.ensure_hiring_agent(loaded)
    fake.results.append(proposal())
    prop = hiring.propose(conn, loaded, "summarise my lecture notes")
    req = fake.requests[0]
    assert req.agent.name == "hiring" and req.json_schema == hiring.SCHEMA
    assert "## Backends you can choose" in req.system_prompt
    assert (prop.name, prop.config.model, prop.config.budget.daily_tokens) == (
        "note-summariser", "haiku", 80000)  # fmt: skip
    assert prop.warnings == []

    from ao.agents import write_agent

    write_agent(loaded, prop.name, prop.config, prop.instructions)
    agent = load_agent(loaded, "note-summariser")
    assert agent.config.role == "Summarises lecture notes"
    assert agent.instructions == "You summarise lecture notes into 5 bullets."


def test_confidential_proposal_warns_when_local_missing(env):
    loaded, conn, fake = env
    hiring.ensure_hiring_agent(loaded)
    fake.results.append(proposal(backend="local", clearance="confidential", model="",
                                 tools=["Read"]))  # fmt: skip
    prop = hiring.propose(conn, loaded, "summarise my private notes")
    assert prop.config.model is None
    assert any("not configured" in w for w in prop.warnings)
    assert any("tools" in w for w in prop.warnings)


def test_name_collision_reasks_once_then_fails(env):
    loaded, conn, fake = env
    hiring.ensure_hiring_agent(loaded)
    fake.results += [proposal(name="hiring"), proposal(name="fresh-name")]
    assert hiring.propose(conn, loaded, "x").name == "fresh-name"
    assert "is taken" in fake.requests[-1].prompt

    fake.results += [proposal(name="hiring"), proposal(name="hiring")]
    with pytest.raises(hiring.HiringError, match="already taken"):
        hiring.propose(conn, loaded, "x")


@pytest.mark.parametrize(
    ("override", "message"),
    [({"name": "Bad Name"}, "invalid"), ({"max_turns": 0}, "limits.max_turns"),
     ({"instructions": " "}, "no instructions"), ({"clearance": "secret"}, "clearance")],
)  # fmt: skip
def test_invalid_proposals(env, override, message):
    loaded, conn, fake = env
    hiring.ensure_hiring_agent(loaded)
    fake.results.append(proposal(**override))
    with pytest.raises(hiring.HiringError, match=message):
        hiring.propose(conn, loaded, "x")


def test_cli_hire_reject_then_accept(env):
    loaded, _, fake = env
    fake.results.append(proposal())
    rejected = cli.invoke(app, ["hire", "summarise lecture notes"], input="n\n")
    assert rejected.exit_code == 0, rejected.output
    assert "created the hiring agent" in rejected.stderr
    assert "# reasoning: Cheap task." in rejected.stdout and 'model = "haiku"' in rejected.stdout
    assert "nothing written" in rejected.stdout
    assert list_agent_names(loaded) == ["hiring"]

    fake.results.append(proposal())
    accepted = cli.invoke(app, ["hire", "summarise lecture notes", "--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert list_agent_names(loaded) == ["hiring", "note-summariser"]
    shown = cli.invoke(app, ["agents", "show", "note-summariser"])
    assert shown.exit_code == 0 and "status:       ok" in shown.stdout


def test_render_config_is_compact(env):
    from ao.agents import render_config

    text = render_config(hiring._to_config(proposal(backend="local", model="").structured))
    assert text.splitlines()[:3] == ['role = "Summarises lecture notes"', 'backend = "local"',
                                     'clearance = "public"']  # fmt: skip
    assert "[limits]" in text and "max_turns = 3" in text
    assert "[memory]" not in text and "add_dirs" not in text
