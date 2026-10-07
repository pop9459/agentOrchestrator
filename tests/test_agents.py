import pytest
from typer.testing import CliRunner

from ao.agents import (
    AgentError,
    agent_problem,
    create_agent,
    list_agent_names,
    load_agent,
    template_names,
)
from ao.cli import app
from ao.config import load_config

runner = CliRunner()


def make_agent(project, name, toml='role = "test agent"\n', instructions="Be terse."):
    directory = project / "agents" / name
    directory.mkdir(parents=True)
    (directory / "agent.toml").write_text(toml)
    if instructions is not None:
        (directory / "INSTRUCTIONS.md").write_text(instructions)
    return directory


def test_load_minimal_agent_uses_defaults(isolated_env):
    make_agent(isolated_env, "scout")
    agent = load_agent(load_config(), "scout")
    assert agent.config.backend == "claude"
    assert agent.config.prompt.mode == "replace"
    assert agent.config.tools.builtin == []
    assert agent.instructions == "Be terse."
    assert agent.memory_index is None
    assert agent.mcp_config is None


def test_workspace_is_outside_the_project(isolated_env):
    make_agent(isolated_env, "scout")
    agent = load_agent(load_config(), "scout")
    assert not agent.workspace.is_relative_to(isolated_env)
    assert agent.workspace.parts[-2:] == ("workspaces", "scout")


def test_optional_files_are_picked_up(isolated_env):
    directory = make_agent(isolated_env, "scout")
    (directory / "memory").mkdir()
    (directory / "memory" / "INDEX.md").write_text("- prefers short answers\n")
    (directory / "mcp.json").write_text('{"mcpServers": {}}')
    agent = load_agent(load_config(), "scout")
    assert agent.memory_index == "- prefers short answers"
    assert agent.mcp_config == directory / "mcp.json"


def test_add_dirs_resolve_relative_to_agent_dir(isolated_env):
    directory = make_agent(isolated_env, "scout", 'role = "r"\n[tools]\nadd_dirs = ["../shared"]\n')
    agent = load_agent(load_config(), "scout")
    assert agent.add_dirs == [(directory / "../shared").resolve()]


def test_invalid_config_reports_dotted_path(isolated_env):
    make_agent(isolated_env, "scout", 'role = "r"\n[limits]\nmax_turn = 3\n')
    with pytest.raises(AgentError, match="limits.max_turn"):
        load_agent(load_config(), "scout")


def test_missing_instructions_is_an_error(isolated_env):
    make_agent(isolated_env, "scout", instructions=None)
    with pytest.raises(AgentError, match="INSTRUCTIONS.md"):
        load_agent(load_config(), "scout")


@pytest.mark.parametrize("name", ["Bad", "../x", "-x", "a b", ""])
def test_invalid_names_rejected(isolated_env, name):
    with pytest.raises(AgentError, match="invalid agent name"):
        load_agent(load_config(), name)


def test_unknown_backend_is_reported_not_raised(isolated_env):
    make_agent(isolated_env, "scout", 'role = "r"\nbackend = "local"\n')
    loaded = load_config()
    assert agent_problem(loaded, load_agent(loaded, "scout")) == "backend 'local' not configured"


def test_every_template_is_valid(isolated_env):
    loaded = load_config()
    for template in template_names():
        create_agent(loaded, f"t-{template}", template)
        load_agent(loaded, f"t-{template}")
    assert {"blank", "jarvis", "linear-manager", "idea-ingestor", "local"} <= set(template_names())


def test_create_agent_defaults_and_refuses_overwrite(isolated_env):
    loaded = load_config()
    create_agent(loaded, "jarvis")  # same-named template
    create_agent(loaded, "helper")  # falls back to blank
    assert "Top-level orchestrator" in load_agent(loaded, "jarvis").config.role
    assert list_agent_names(loaded) == ["helper", "jarvis"]
    with pytest.raises(AgentError, match="already exists"):
        create_agent(loaded, "jarvis")
    with pytest.raises(AgentError, match="unknown template"):
        create_agent(loaded, "x", "nope")


def test_cli_init_list_show(isolated_env):
    assert "ao agents init" in runner.invoke(app, ["agents", "list"]).output

    result = runner.invoke(app, ["agents", "init"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, ["agents", "init"]).output == ""  # idempotent

    listing = runner.invoke(app, ["agents", "list"]).output
    assert "jarvis" in listing and "claude/sonnet" in listing
    assert "backend 'local' not configured" in listing
    assert "blank" not in listing

    shown = runner.invoke(app, ["agents", "show", "idea-ingestor"])
    assert shown.exit_code == 0
    assert '"model": "haiku"' in shown.output
    assert runner.invoke(app, ["agents", "show", "ghost"]).exit_code == 2


def test_cli_list_survives_broken_agent(isolated_env):
    make_agent(isolated_env, "broken", "role = 1\n")
    result = runner.invoke(app, ["agents", "list"])
    assert result.exit_code == 0
    assert "invalid" in result.output
