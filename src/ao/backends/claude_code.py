"""Claude Code CLI backend: one headless `claude -p` process per run.

Flag choices are measured, not guessed (KAP-102): replace-mode system prompt, no tools,
strict (empty) MCP config, no skills listing and a cwd outside the repo keep a trivial
run at about 430 input tokens instead of about 20k for a bare `claude -p`.
Verify flags against `claude --help` when upgrading Claude Code.
"""

import json
import os
import shlex
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from ao import mcp, secrets
from ao.backends import register
from ao.backends.base import Health, Result, RunRequest, Usage
from ao.config import BackendConfig
from ao.secrets import ENV_PREFIX

# Always passed. Headless and deterministic: nothing may prompt for permission, no
# session files, no MCP servers except the agent's own, no skill listing.
# --restricted (KAP-86, measured: no token cost) ignores the user's settings files, so
# their allow-rules can't widen an agent's permissions, and confines file tools to the
# workspace + --add-dir. Under dontAsk, reads there are allowed; writes/edits/Bash need
# an explicit `tools.allow` entry; everything else is denied without prompting.
FIXED_FLAGS = (
    "-p",
    "--output-format", "json",
    "--no-session-persistence",
    "--permission-prompts", "none",
    "--permission-mode", "dontAsk",
    "--restricted",
    "--strict-mcp-config",
    "--disable-slash-commands",
)  # fmt: skip

# Removed from the child env when `use_subscription` is on, so runs bill the Pro plan.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
STDERR_TAIL = 2000


def build_argv(
    config: BackendConfig, request: RunRequest, mcp_config: Path | None = None
) -> list[str]:
    """The full command line for a run. The user prompt is sent on stdin, not here.

    `mcp_config` overrides the agent's mcp.json path (used for the rendered temp copy).
    """
    agent = request.agent.config
    argv = [*shlex.split(config.command or "claude"), *FIXED_FLAGS]
    if request.model:
        argv += ["--model", request.model]
    prompt_flag = "--system-prompt" if agent.prompt.mode == "replace" else "--append-system-prompt"
    argv += [prompt_flag, request.system_prompt]
    argv += ["--tools", ",".join(agent.tools.builtin)]  # "" disables every built-in tool
    if agent.tools.allow:
        argv += ["--allowedTools", ",".join(agent.tools.allow)]
    if agent.tools.deny:
        argv += ["--disallowedTools", ",".join(agent.tools.deny)]
    for directory in request.agent.add_dirs:
        argv += ["--add-dir", str(directory)]
    # Structured output costs one extra turn (claude returns it via an internal tool call).
    turns = (
        max(agent.limits.max_turns, 2)
        if request.json_schema is not None
        else agent.limits.max_turns
    )
    argv += ["--max-turns", str(turns)]
    if agent.limits.effort:
        argv += ["--effort", agent.limits.effort]
    if request.max_cost_usd is not None:
        argv += ["--max-budget-usd", f"{request.max_cost_usd:g}"]
    if request.json_schema is not None:
        argv += ["--json-schema", json.dumps(request.json_schema)]
    mcp_config = mcp_config or request.agent.mcp_config
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config)]
    return argv


def child_env(config: BackendConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for var in list(env):
        if var.startswith(ENV_PREFIX) or (config.use_subscription and var in API_KEY_VARS):
            del env[var]
    return env


def _main_model(model_usage: dict[str, Any]) -> str | None:
    """The model that produced most output (side calls to smaller models are common)."""
    if not model_usage:
        return None
    return max(model_usage, key=lambda m: model_usage[m].get("outputTokens", 0))


def parse_result(stdout: str, returncode: int, stderr: str = "") -> Result:
    """Map `claude -p --output-format json` output to a Result."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).strip()[-STDERR_TAIL:] or "no output"
        return Result(outcome="error", error=f"exit {returncode}: {detail}")
    if not isinstance(data, dict) or data.get("type") != "result":
        return Result(outcome="error", error=f"unexpected output: {stdout[:200]}", raw=None)

    raw_usage = data.get("usage") or {}
    usage = Usage(
        input_tokens=raw_usage.get("input_tokens", 0),
        output_tokens=raw_usage.get("output_tokens", 0),
        cache_read_tokens=raw_usage.get("cache_read_input_tokens", 0),
        cache_write_tokens=raw_usage.get("cache_creation_input_tokens", 0),
        cost_usd=data.get("total_cost_usd"),
    )
    failed = data.get("is_error") or data.get("subtype") != "success" or returncode != 0
    error = None
    if failed:
        reason = data.get("subtype") or "error"
        detail = data.get("result") or data.get("api_error_status") or stderr.strip()[-500:]
        error = f"{reason}: {detail}" if detail else reason
    return Result(
        outcome="error" if failed else "ok",
        text=data.get("result") or "",
        usage=usage,
        model=_main_model(data.get("modelUsage") or {}),
        session_id=data.get("session_id"),
        num_turns=data.get("num_turns"),
        duration_ms=data.get("duration_ms"),
        error=error,
        raw=data,
        structured=structured
        if isinstance(structured := data.get("structured_output"), dict)
        else None,
    )


class ClaudeCodeBackend:
    def __init__(self, name: str, config: BackendConfig) -> None:
        self.name = name
        self.config = config

    @property
    def is_local(self) -> bool:
        return False  # always Anthropic's cloud, whatever the config says

    def _timeout(self, request: RunRequest) -> float:
        return request.agent.config.limits.timeout_s or self.config.timeout_s

    def run(self, request: RunRequest) -> Result:
        try:
            with mcp.materialize(request.agent.mcp_config) as mcp_config:
                return self._run(build_argv(self.config, request, mcp_config), request)
        except secrets.SecretNotFound as exc:
            return Result(outcome="error", error=str(exc))

    def _run(self, argv: list[str], request: RunRequest) -> Result:
        cwd = request.agent.workspace
        cwd.mkdir(parents=True, exist_ok=True)
        timeout = self._timeout(request)
        started = time.monotonic()
        try:
            # Own process group so a timeout also kills anything claude spawned (MCP servers).
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=child_env(self.config),
                start_new_session=True,
            )
        except FileNotFoundError:
            return Result(outcome="error", error=f"command not found: {argv[0]}")
        try:
            stdout, stderr = proc.communicate(request.prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return Result(
                outcome="timeout",
                error=f"no result after {timeout:g}s; process killed",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        result = parse_result(stdout, proc.returncode, stderr)
        if result.duration_ms is None:
            result = replace(result, duration_ms=int((time.monotonic() - started) * 1000))
        return result

    def health(self) -> Health:
        argv = [*shlex.split(self.config.command or "claude"), "--version"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return Health(ok=False, detail=f"{argv[0]}: {exc}")
        output = (proc.stdout or proc.stderr).strip()
        return Health(ok=proc.returncode == 0, detail=output or f"exit {proc.returncode}")

    def preview(self, request: RunRequest) -> str:
        argv = build_argv(self.config, request)
        shown = [
            f"<system prompt: {len(arg)} chars>" if arg == request.system_prompt else arg
            for arg in argv
        ]
        return shlex.join(shown) + "  < prompt on stdin"


@register("claude_code")
def _factory(name: str, config: BackendConfig) -> ClaudeCodeBackend:
    return ClaudeCodeBackend(name, config)
