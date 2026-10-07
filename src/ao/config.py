"""Typed configuration merged from TOML files.

Merge order (later wins): built-in `DEFAULTS` < user `~/.config/ao/ao.toml`
< project `ao.toml` < project `ao.local.toml` (gitignored).

Config never holds secret values, only secret *names* (e.g. `api_key_secret = "llama"`)
that `ao.secrets.get_secret` resolves at use time.
"""

import copy
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ao.paths import DataDirs, config_dir, find_project_root, resolve_data_dir

SECRET_REF = r"^[A-Za-z0-9_.-]{1,64}$"


class ConfigError(Exception):
    """Config could not be read or failed validation."""


class StrictModel(BaseModel):
    # Unknown keys are errors, so typos surface instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")


class PathsConfig(StrictModel):
    agents_dir: Path = Path("agents")
    data_dir: Path | None = None


class BackendConfig(StrictModel):
    type: Literal["claude_code", "openai_compat", "cli_template"]
    model: str | None = None
    base_url: str | None = None
    command: str | None = None
    api_key_secret: str | None = Field(default=None, pattern=SECRET_REF)
    is_local: bool = False
    timeout_s: float = Field(default=300, gt=0)
    # claude_code: drop ANTHROPIC_API_KEY from the child env so runs bill the Pro subscription.
    use_subscription: bool = True


class BudgetsConfig(StrictModel):
    global_daily_tokens: int | None = Field(default=None, ge=0)


class AoConfig(StrictModel):
    paths: PathsConfig = PathsConfig()
    backends: dict[str, BackendConfig] = {}
    budgets: BudgetsConfig = BudgetsConfig()


# Base layer under all config files; a `claude` backend works out of the box.
DEFAULTS: dict[str, Any] = {"backends": {"claude": {"type": "claude_code", "command": "claude"}}}


@dataclass(frozen=True)
class LoadedConfig:
    config: AoConfig
    project_root: Path
    sources: list[Path]

    @property
    def agents_dir(self) -> Path:
        path = self.config.paths.agents_dir.expanduser()
        return path if path.is_absolute() else self.project_root / path

    @property
    def data(self) -> DataDirs:
        return resolve_data_dir(self.config.paths.data_dir, self.project_root)

    @property
    def workspaces_dir(self) -> Path:
        # Outside the repo on purpose: a cwd inside it makes Claude Code auto-load the
        # repo CLAUDE.md into every agent run (KAP-102).
        return self.data.root / "workspaces"


def config_files(project_root: Path) -> list[Path]:
    """Candidate config files in merge order (lowest precedence first)."""
    return [config_dir() / "ao.toml", project_root / "ao.toml", project_root / "ao.local.toml"]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _record_origins(data: dict[str, Any], source: Path, origins: dict[str, Path], prefix=""):
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        origins[dotted] = source
        if isinstance(value, dict):
            _record_origins(value, source, origins, f"{dotted}.")


def _origin_for(dotted: str, origins: dict[str, Path]) -> Path | None:
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        if (found := origins.get(".".join(parts[:i]))) is not None:
            return found
    return None


def format_validation_error(
    err: ValidationError, origins: dict[str, Path] | None = None, title="Invalid configuration:"
) -> str:
    """One line per issue: dotted key path, message and (if known) the file it came from."""
    origins = origins or {}
    lines = [title]
    for issue in err.errors():
        dotted = ".".join(str(part) for part in issue["loc"])
        origin = _origin_for(dotted, origins)
        where = f" (from {origin})" if origin else ""
        lines.append(f"  {dotted}: {issue['msg']}{where}")
    return "\n".join(lines)


def load_config(project_root: Path | None = None) -> LoadedConfig:
    root = project_root or find_project_root()
    merged: dict[str, Any] = copy.deepcopy(DEFAULTS)
    origins: dict[str, Path] = {}
    sources: list[Path] = []
    for path in config_files(root):
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
        merged = deep_merge(merged, data)
        _record_origins(data, path, origins)
        sources.append(path)
    try:
        config = AoConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, origins)) from exc
    return LoadedConfig(config=config, project_root=root, sources=sources)


_SENSITIVE = re.compile(r"secret|key|token|password", re.IGNORECASE)
REDACTED = "***"


def redact(value: Any, key: str = "") -> Any:
    """Mask values under sensitive-looking keys for display.

    `*_secret` fields are exempt: by contract they hold a secret *name*, not a value.
    """
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if value is not None and _SENSITIVE.search(key) and not key.endswith("_secret"):
        return REDACTED
    return value
