"""Filesystem locations: project root, XDG config dir and data dirs."""

import os
from dataclasses import dataclass
from pathlib import Path

APP = "ao"
PROJECT_MARKERS = ("ao.toml", "ao.local.toml", ".git")


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    """User-level config dir, `$XDG_CONFIG_HOME/ao`."""
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def default_data_dir() -> Path:
    """Default data dir, `$XDG_DATA_HOME/ao`."""
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def find_project_root(start: Path | None = None) -> Path:
    """Nearest ancestor of `start` (default: cwd) containing an ao config or `.git`.

    Falls back to `start` itself when no marker is found.
    """
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            return directory
    return start


@dataclass(frozen=True)
class DataDirs:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "db"

    @property
    def db_path(self) -> Path:
        return self.db / "ao.sqlite3"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> "DataDirs":
        for directory in (self.db, self.cache, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def resolve_data_dir(configured: Path | None, project_root: Path) -> DataDirs:
    """`AO_DATA_DIR` env wins, then the configured path (relative to the project root),
    then the XDG default."""
    if env := os.environ.get("AO_DATA_DIR"):
        return DataDirs(Path(env).expanduser())
    if configured is not None:
        configured = configured.expanduser()
        return DataDirs(configured if configured.is_absolute() else project_root / configured)
    return DataDirs(default_data_dir())
