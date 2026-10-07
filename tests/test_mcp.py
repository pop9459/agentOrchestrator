import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from ao import mcp
from ao.agents import AgentError, create_agent, load_agent
from ao.backends.base import RunRequest
from ao.backends.claude_code import ClaudeCodeBackend, build_argv
from ao.config import BackendConfig, load_config
from ao.run import RunError, prepare

FIXTURE = Path(__file__).parent / "fixtures" / "claude_result_ok.json"
LINEAR = {
    "mcpServers": {
        "linear": {
            "type": "http",
            "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": "Bearer {{secret:linear}}"},
        }
    }
}


@pytest.fixture
def loaded(isolated_env):
    config = load_config()
    create_agent(config, "scout", "blank")
    return config


def write_mcp(loaded, data):
    path = loaded.agents_dir / "scout" / "mcp.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_no_mcp_json_means_strict_and_no_servers(loaded):
    agent = load_agent(loaded, "scout")
    argv = build_argv(BackendConfig(type="claude_code"),
                      RunRequest(agent=agent, system_prompt="s", prompt="p"))  # fmt: skip
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_mcp_json_without_secrets_is_passed_as_is(loaded):
    path = write_mcp(loaded, {"mcpServers": {"x": {"command": "x-server"}}})
    agent = load_agent(loaded, "scout")
    assert agent.mcp_config == path
    with mcp.materialize(path) as rendered:
        assert rendered == path
    argv = build_argv(BackendConfig(type="claude_code"),
                      RunRequest(agent=agent, system_prompt="s", prompt="p"))  # fmt: skip
    assert argv[argv.index("--mcp-config") + 1] == str(path)
    assert "--strict-mcp-config" in argv


@pytest.mark.parametrize("bad", ["{nope", "[]", '{"servers": {}}', '{"mcpServers": {"x": 1}}'])
def test_invalid_mcp_json_rejected_at_load(loaded, bad):
    write_mcp(loaded, bad)
    with pytest.raises(AgentError, match="mcp.json"):
        load_agent(loaded, "scout")


def test_placeholders_render_into_private_temp_file(loaded, monkeypatch):
    path = write_mcp(loaded, LINEAR)
    monkeypatch.setenv("AO_SECRET_LINEAR", 'lin"key')
    assert mcp.secret_names(path) == {"linear"}
    with mcp.materialize(path) as rendered:
        assert rendered != path
        assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
        data = json.loads(rendered.read_text())
        assert data["mcpServers"]["linear"]["headers"]["Authorization"] == 'Bearer lin"key'
    assert not rendered.exists()
    assert "{{secret:linear}}" in path.read_text()  # original untouched


def test_missing_secret_fails_in_prepare(loaded, monkeypatch):
    write_mcp(loaded, LINEAR)
    with pytest.raises(RunError, match="AO_SECRET_LINEAR"):
        prepare(loaded, "scout", "hi")


def test_backend_passes_rendered_file_and_cleans_up(loaded, monkeypatch, tmp_path):
    write_mcp(loaded, LINEAR)
    monkeypatch.setenv("AO_SECRET_LINEAR", "lin-key")
    script = tmp_path / "fake_claude.py"
    script.write_text(
        textwrap.dedent(f"""
        import json, sys
        sys.stdin.read()
        path = sys.argv[sys.argv.index("--mcp-config") + 1]
        data = json.load(open({str(FIXTURE)!r}))
        data["result"] = path + "|" + open(path).read()
        print(json.dumps(data))
    """)
    )
    backend = ClaudeCodeBackend(
        "claude", BackendConfig(type="claude_code", command=f"{sys.executable} {script}")
    )
    agent = load_agent(loaded, "scout")
    result = backend.run(RunRequest(agent=agent, system_prompt="s", prompt="p"))
    assert result.outcome == "ok", result.error
    used_path, content = result.text.split("|", 1)
    assert "Bearer lin-key" in content
    assert not Path(used_path).exists()
