import json
import os
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from ao.agents import create_agent, load_agent
from ao.backends import get_backend
from ao.backends.base import RunRequest
from ao.backends.claude_code import (
    FIXED_FLAGS,
    ClaudeCodeBackend,
    build_argv,
    child_env,
    parse_result,
)
from ao.config import BackendConfig, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "claude_result_ok.json"


def flag(argv, name):
    """Value following `name` in argv (None if absent)."""
    return argv[argv.index(name) + 1] if name in argv else None


@pytest.fixture
def agent(isolated_env):
    loaded = load_config()
    create_agent(loaded, "scout", "blank")
    return load_agent(loaded, "scout")


def request_for(agent, **overrides):
    defaults = dict(agent=agent, system_prompt="SYS", prompt="hello", model="haiku")
    return RunRequest(**{**defaults, **overrides})


def with_config(agent, **sections):
    config = agent.config.model_copy(
        update={k: getattr(agent.config, k).model_copy(update=v) for k, v in sections.items()}
    )
    return replace(agent, config=config)


# --- argv ------------------------------------------------------------------------------


def test_registry_builds_claude_backend():
    backend = get_backend("claude", BackendConfig(type="claude_code"))
    assert isinstance(backend, ClaudeCodeBackend)
    assert backend.is_local is False


def test_minimal_argv_uses_cheapest_flags(agent):
    argv = build_argv(BackendConfig(type="claude_code"), request_for(agent))
    assert argv[0] == "claude"
    assert tuple(argv[1 : 1 + len(FIXED_FLAGS)]) == FIXED_FLAGS
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert "--restricted" in argv and flag(argv, "--permission-mode") == "dontAsk"
    assert flag(argv, "--system-prompt") == "SYS"
    assert "--append-system-prompt" not in argv
    assert flag(argv, "--tools") == ""
    assert flag(argv, "--model") == "haiku"
    assert flag(argv, "--max-turns") == "4"
    assert "hello" not in argv  # prompt goes via stdin
    for absent in ("--allowedTools", "--disallowedTools", "--add-dir", "--effort",
                   "--max-budget-usd", "--json-schema", "--mcp-config"):  # fmt: skip
        assert absent not in argv


def test_full_argv_mapping(agent, tmp_path):
    agent = with_config(
        agent,
        prompt={"mode": "append"},
        tools={"builtin": ["Read", "Grep"], "allow": ["Bash(git status)", "Read"],
               "deny": ["WebFetch"], "add_dirs": [tmp_path]},
        limits={"max_turns": 3, "effort": "low"},
    )  # fmt: skip
    req = request_for(agent, max_cost_usd=0.25, json_schema={"type": "object"})
    argv = build_argv(BackendConfig(type="claude_code", command="/opt/claude --x"), req)
    assert argv[:2] == ["/opt/claude", "--x"]
    assert flag(argv, "--append-system-prompt") == "SYS"
    assert flag(argv, "--tools") == "Read,Grep"
    assert flag(argv, "--allowedTools") == "Bash(git status),Read"
    assert flag(argv, "--disallowedTools") == "WebFetch"
    assert flag(argv, "--add-dir") == str(tmp_path)
    assert flag(argv, "--max-turns") == "3"
    assert flag(argv, "--effort") == "low"
    assert flag(argv, "--max-budget-usd") == "0.25"
    assert json.loads(flag(argv, "--json-schema")) == {"type": "object"}


def test_child_env_scrubs_secrets_and_api_keys():
    base = {"PATH": "/bin", "AO_SECRET_LLAMA": "s", "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_AUTH_TOKEN": "t", "HOME": "/h"}  # fmt: skip
    assert child_env(BackendConfig(type="claude_code"), base) == {"PATH": "/bin", "HOME": "/h"}
    kept = child_env(BackendConfig(type="claude_code", use_subscription=False), base)
    assert "ANTHROPIC_API_KEY" in kept and "AO_SECRET_LLAMA" not in kept


def test_preview_hides_system_prompt_value(agent):
    backend = ClaudeCodeBackend("claude", BackendConfig(type="claude_code"))
    preview = backend.preview(request_for(agent, system_prompt="very long " * 10))
    assert "<system prompt: 100 chars>" in preview
    assert "very long" not in preview


# --- parsing -----------------------------------------------------------------------------


def test_parse_real_success_output():
    result = parse_result(FIXTURE.read_text(), 0)
    assert result.outcome == "ok"
    assert result.text == "ok"
    assert result.model == "claude-haiku-4-5-20251001"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (429, 43)
    assert result.usage.cost_usd == pytest.approx(0.000644)
    assert result.num_turns == 1
    assert result.duration_ms == 1004
    assert result.error is None


def test_parse_error_result():
    data = json.loads(FIXTURE.read_text())
    data.update(is_error=True, subtype="error_max_turns", result="")
    result = parse_result(json.dumps(data), 1)
    assert result.outcome == "error"
    assert result.error.startswith("error_max_turns")
    assert result.usage.input_tokens == 429  # usage still recorded


def test_parse_garbage():
    result = parse_result("Not logged in", 1, stderr="please run claude auth")
    assert result.outcome == "error"
    assert "please run claude auth" in result.error


def test_main_model_is_the_one_with_most_output():
    data = json.loads(FIXTURE.read_text())
    data["modelUsage"] = {"small": {"outputTokens": 5}, "big": {"outputTokens": 50}}
    assert parse_result(json.dumps(data), 0).model == "big"


# --- process handling with a fake `claude` -------------------------------------------------


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Backend whose command is a Python script; behaviour picked via FAKE_CLAUDE_MODE."""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        textwrap.dedent(f"""
        import json, os, sys, time
        mode = os.environ["FAKE_CLAUDE_MODE"]
        prompt = sys.stdin.read()
        if mode == "ok":
            data = json.load(open({str(FIXTURE)!r}))
            data["result"] = "got: " + prompt + " cwd=" + os.getcwd()
            data["result"] += " key=" + str("ANTHROPIC_API_KEY" in os.environ)
            print(json.dumps(data))
        elif mode == "crash":
            print("boom", file=sys.stderr); sys.exit(3)
        elif mode == "sleep":
            time.sleep(30)
    """)
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    return ClaudeCodeBackend(
        "claude", BackendConfig(type="claude_code", command=f"{sys.executable} {script}")
    )


def test_run_success_uses_stdin_cwd_and_scrubbed_env(agent, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    result = fake_claude.run(request_for(agent, prompt="hi there"))
    assert result.outcome == "ok", result.error
    assert result.text == f"got: hi there cwd={agent.workspace} key=False"
    assert agent.workspace.is_dir()


def test_run_nonzero_exit(agent, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    result = fake_claude.run(request_for(agent))
    assert result.outcome == "error"
    assert "exit 3: boom" in result.error


def test_run_timeout_kills_process(agent, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    slow = with_config(agent, limits={"timeout_s": 0.5})
    result = fake_claude.run(request_for(slow))
    assert result.outcome == "timeout"
    assert result.duration_ms < 5000


def test_missing_command(agent):
    backend = ClaudeCodeBackend(
        "claude", BackendConfig(type="claude_code", command="no-such-claude")
    )
    assert backend.run(request_for(agent)).error == "command not found: no-such-claude"
    assert backend.health().ok is False


# --- live (opt-in: AO_LIVE=1; uses a tiny amount of the Pro quota) ------------------------


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("AO_LIVE") != "1", reason="set AO_LIVE=1 to call real claude")
def test_live_minimal_run(agent):
    backend = ClaudeCodeBackend("claude", BackendConfig(type="claude_code"))
    assert backend.health().ok
    result = backend.run(
        request_for(agent, system_prompt="Reply with exactly: pong", prompt="ping")
    )
    assert result.outcome == "ok", result.error
    assert "pong" in result.text.lower()
    assert 0 < result.usage.input_tokens + result.usage.cache_read_tokens < 3000


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("AO_LIVE") != "1", reason="set AO_LIVE=1 to call real claude")
def test_live_read_outside_workspace_is_denied(agent, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("OUTSIDE-MARK-456")
    inside = agent.workspace / "inside.txt"
    agent.workspace.mkdir(parents=True, exist_ok=True)
    inside.write_text("INSIDE-MARK-123")
    reader = with_config(agent, tools={"builtin": ["Read"]})
    backend = ClaudeCodeBackend("claude", BackendConfig(type="claude_code"))
    system = "Use the Read tool when asked and reply with the exact file content or error."

    ok = backend.run(request_for(reader, system_prompt=system, prompt="Read ./inside.txt"))
    assert "INSIDE-MARK-123" in ok.text
    denied = backend.run(request_for(reader, system_prompt=system, prompt=f"Read {secret}"))
    assert "OUTSIDE-MARK-456" not in denied.text
    assert denied.raw["permission_denials"]
