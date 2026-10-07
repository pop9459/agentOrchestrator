"""Agent definitions: one directory per agent under `paths.agents_dir`.

    agents/<name>/
        agent.toml        backend, model, prompt mode, tools, limits, budget (AgentConfig)
        INSTRUCTIONS.md   the agent's system prompt
        mcp.json          optional; the only MCP servers this agent gets
        memory/INDEX.md   optional; appended to the system prompt

The whole agents dir is user data (gitignored). Generic starting points ship as
package templates in `ao/agent_templates/` and are copied by `ao agents new/init`.
The run workspace (cwd) lives outside the repo in `<data>/workspaces/<name>`.
"""

import re
import shutil
import tomllib
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import Field, ValidationError

from ao import mcp
from ao.config import LoadedConfig, StrictModel, format_validation_error

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
AGENT_FILE = "agent.toml"
INSTRUCTIONS_FILE = "INSTRUCTIONS.md"
MCP_FILE = "mcp.json"
MEMORY_INDEX = Path("memory") / "INDEX.md"


class AgentError(Exception):
    """Agent missing, misnamed or invalid."""


class PromptConfig(StrictModel):
    # replace: INSTRUCTIONS become the whole system prompt (smallest, no tool guidance).
    # append: Claude Code's default system prompt + INSTRUCTIONS (needed for tool use).
    mode: Literal["replace", "append"] = "replace"


class ToolsConfig(StrictModel):
    builtin: list[str] = []  # built-in tools available at all; [] = none
    allow: list[str] = []  # auto-approved tool patterns, e.g. "Bash(git status)"
    deny: list[str] = []
    add_dirs: list[Path] = []  # extra dirs the tools may touch; relative to the agent dir


class LimitsConfig(StrictModel):
    timeout_s: float | None = Field(default=None, gt=0)  # None: backend default
    max_turns: int = Field(default=8, ge=1)
    max_context_tokens: int = Field(default=30_000, ge=100)  # estimate; over → refused
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class BudgetConfig(StrictModel):
    daily_tokens: int | None = Field(default=None, ge=0)
    daily_cost_usd: float | None = Field(default=None, ge=0)
    per_run_cost_usd: float | None = Field(default=None, gt=0)


class MemoryConfig(StrictModel):
    writeback: bool = False  # agent may propose memory ops (adds a structured-output turn)
    max_facts: int = Field(default=40, ge=1, le=500)


class ContextConfig(StrictModel):
    # Extra system sections added on every run (see ao.context_providers).
    providers: list[Literal["linear_board"]] = []


class AgentConfig(StrictModel):
    role: str = Field(min_length=1)
    backend: str = "claude"
    model: str | None = None
    clearance: Literal["public", "confidential"] = "public"
    prompt: PromptConfig = PromptConfig()
    tools: ToolsConfig = ToolsConfig()
    limits: LimitsConfig = LimitsConfig()
    budget: BudgetConfig = BudgetConfig()
    memory: MemoryConfig = MemoryConfig()
    context: ContextConfig = ContextConfig()


@dataclass(frozen=True)
class Agent:
    name: str
    dir: Path
    config: AgentConfig
    instructions: str
    memory_index: str | None
    mcp_config: Path | None
    workspace: Path

    @property
    def add_dirs(self) -> list[Path]:
        return [
            p if p.is_absolute() else (self.dir / p).resolve() for p in self.config.tools.add_dirs
        ]


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise AgentError(f"invalid agent name {name!r}: use lowercase letters, digits and '-'")
    return name


def agent_dir(loaded: LoadedConfig, name: str) -> Path:
    return loaded.agents_dir / validate_name(name)


def list_agent_names(loaded: LoadedConfig) -> list[str]:
    root = loaded.agents_dir
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir() if (d / AGENT_FILE).is_file() and NAME_RE.match(d.name)
    )


def load_agent(loaded: LoadedConfig, name: str) -> Agent:
    directory = agent_dir(loaded, name)
    config_path = directory / AGENT_FILE
    if not config_path.is_file():
        raise AgentError(f"agent {name!r} not found ({config_path} missing)")
    try:
        data = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise AgentError(f"{config_path}: invalid TOML: {exc}") from exc
    try:
        config = AgentConfig.model_validate(data)
    except ValidationError as exc:
        raise AgentError(
            format_validation_error(exc, title=f"Invalid agent config {config_path}:")
        ) from exc

    instructions_path = directory / INSTRUCTIONS_FILE
    if not instructions_path.is_file():
        raise AgentError(f"agent {name!r} has no {INSTRUCTIONS_FILE}")
    _check_add_dirs(loaded, directory, config.tools.add_dirs)
    memory_path = directory / MEMORY_INDEX
    mcp_path = directory / MCP_FILE
    if mcp_path.is_file():
        try:
            mcp.validate(mcp_path)
        except mcp.McpConfigError as exc:
            raise AgentError(str(exc)) from exc
    return Agent(
        name=name,
        dir=directory,
        config=config,
        instructions=instructions_path.read_text().strip(),
        memory_index=memory_path.read_text().strip() if memory_path.is_file() else None,
        mcp_config=mcp_path if mcp_path.is_file() else None,
        workspace=loaded.workspaces_dir / name,
    )


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _check_add_dirs(loaded: LoadedConfig, agent_dir_: Path, add_dirs: list[Path]) -> None:
    """Extra dirs must not expose ao's own state: other agents (instructions, memory),
    the DB or other workspaces. Ancestors of those (e.g. the repo or $HOME) count too."""
    protected = {
        "the agents dir": loaded.agents_dir.resolve(),
        "the ao data dir": loaded.data.root.resolve(),
    }
    for raw in add_dirs:
        resolved = (raw if raw.is_absolute() else agent_dir_ / raw).expanduser().resolve()
        for label, path in protected.items():
            if _overlaps(resolved, path):
                raise AgentError(f"tools.add_dirs entry {str(raw)!r} overlaps {label} ({path})")


def agent_problem(loaded: LoadedConfig, agent: Agent) -> str | None:
    """A reason the agent cannot run right now, or None."""
    if agent.config.backend not in loaded.config.backends:
        return f"backend {agent.config.backend!r} not configured"
    return None


def render_config(config: AgentConfig) -> str:
    """Compact agent.toml text: defaults are omitted, except the key identity fields."""
    data = config.model_dump(mode="json", exclude_defaults=True)
    head = {"role": config.role, "backend": config.backend}
    if config.model:
        head["model"] = config.model
    head["clearance"] = config.clearance
    return tomli_w.dumps({**head, **{k: v for k, v in data.items() if k not in head}})


def write_agent(loaded: LoadedConfig, name: str, config: AgentConfig, instructions: str) -> Path:
    """Create `agents/<name>/` from a config; refuses to overwrite, rolls back if invalid."""
    directory = agent_dir(loaded, name)
    if directory.exists():
        raise AgentError(f"agent {name!r} already exists at {directory}")
    directory.mkdir(parents=True)
    try:
        (directory / AGENT_FILE).write_text(render_config(config))
        (directory / INSTRUCTIONS_FILE).write_text(instructions.strip() + "\n")
        load_agent(loaded, name)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory


# --- templates -------------------------------------------------------------------------


def _templates() -> Traversable:
    return resources.files("ao") / "agent_templates"


def template_names() -> list[str]:
    return sorted(t.name for t in _templates().iterdir() if (t / AGENT_FILE).is_file())


def _copy_tree(src: Traversable, dest: Path) -> None:
    dest.mkdir(parents=True)
    for item in src.iterdir():
        if item.is_dir():
            _copy_tree(item, dest / item.name)
        else:
            (dest / item.name).write_bytes(item.read_bytes())


def create_agent(loaded: LoadedConfig, name: str, template: str | None = None) -> Path:
    """Create `agents/<name>/` from a template (defaults to the template of the same name,
    else `blank`). Refuses to overwrite."""
    directory = agent_dir(loaded, name)
    if directory.exists():
        raise AgentError(f"agent {name!r} already exists at {directory}")
    template = template or (name if name in template_names() else "blank")
    if template not in template_names():
        raise AgentError(f"unknown template {template!r}; available: {', '.join(template_names())}")
    try:
        _copy_tree(_templates() / template, directory)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory
