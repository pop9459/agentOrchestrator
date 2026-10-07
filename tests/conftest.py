import os
from pathlib import Path

import pytest

from ao import secrets


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep tests away from the real home dir, env secrets and OS keyring.

    Returns a fresh project dir (containing `.git`) that is also the cwd.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("AO_DATA_DIR", raising=False)
    for var in list(os.environ):
        if var.startswith(secrets.ENV_PREFIX):
            monkeypatch.delenv(var)
    monkeypatch.setattr(secrets, "_keyring_get", lambda name: None)
    secrets._dotenv.clear()

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.chdir(project)
    return project
