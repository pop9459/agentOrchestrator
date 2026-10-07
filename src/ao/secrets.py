"""Secret lookup: env var > project `.env` > OS keyring (optional `keyring` extra).

A secret named `llama` is read from `AO_SECRET_LLAMA`, then from the same key in the
project's `.env`, then from the keyring entry service="ao", username="llama".

`.env` values are kept in this module and never copied into `os.environ`, so they
are not inherited by agent subprocesses.
"""

import os
import re
from pathlib import Path

from dotenv import dotenv_values

ENV_PREFIX = "AO_SECRET_"
KEYRING_SERVICE = "ao"

_dotenv: dict[str, str] = {}


class SecretNotFound(LookupError):
    pass


def env_var_name(name: str) -> str:
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def load_dotenv_file(project_root: Path) -> None:
    """(Re)load secrets from `<project_root>/.env`; missing file means no values."""
    _dotenv.clear()
    path = project_root / ".env"
    if path.is_file():
        _dotenv.update({k: v for k, v in dotenv_values(path).items() if v is not None})


def _keyring_get(name: str) -> str | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # no backend / locked wallet: treat as absent
        return None


def _lookup(name: str) -> tuple[str, str] | None:
    var = env_var_name(name)
    if value := os.environ.get(var):
        return value, "env"
    if value := _dotenv.get(var):
        return value, ".env"
    if value := _keyring_get(name):
        return value, "keyring"
    return None


def get_secret(name: str) -> str:
    found = _lookup(name)
    if found is None:
        raise SecretNotFound(
            f"Secret '{name}' not found. Set {env_var_name(name)} in the environment or .env, "
            f"or run: keyring set {KEYRING_SERVICE} {name}"
        )
    return found[0]


def secret_source(name: str) -> str | None:
    """Where a secret would be read from ('env', '.env', 'keyring'), without revealing it."""
    found = _lookup(name)
    return found[1] if found else None
