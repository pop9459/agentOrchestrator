import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ao import secrets
from ao.cli import app
from ao.config import REDACTED, ConfigError, load_config, redact
from ao.paths import config_dir, find_project_root

runner = CliRunner()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_defaults_without_files(isolated_env):
    loaded = load_config()
    assert loaded.sources == []
    assert set(loaded.config.backends) == {"claude"}  # built-in default
    assert loaded.config.backends["claude"].type == "claude_code"
    assert loaded.agents_dir == isolated_env / "agents"
    assert loaded.data.root == isolated_env.parent / "xdg-data" / "ao"


def test_user_backends_merge_with_builtin_claude(isolated_env):
    write(isolated_env / "ao.toml", '[backends.local]\ntype = "openai_compat"\n'
          '[backends.claude]\nmodel = "haiku"\n')  # fmt: skip
    backends = load_config().config.backends
    assert set(backends) == {"claude", "local"}
    assert backends["claude"].type == "claude_code"
    assert backends["claude"].model == "haiku"


def test_merge_order_user_project_local(isolated_env):
    write(
        config_dir() / "ao.toml",
        '[backends.claude]\ntype = "claude_code"\nmodel = "haiku"\n'
        "[budgets]\nglobal_daily_tokens = 100\n",
    )
    write(isolated_env / "ao.toml", '[backends.claude]\ntype = "claude_code"\nmodel = "sonnet"\n')
    write(isolated_env / "ao.local.toml", "[budgets]\nglobal_daily_tokens = 5\n")

    loaded = load_config()
    assert [p.name for p in loaded.sources] == ["ao.toml", "ao.toml", "ao.local.toml"]
    assert loaded.config.backends["claude"].model == "sonnet"  # project beats user
    assert loaded.config.budgets.global_daily_tokens == 5  # local beats user


def test_deep_merge_keeps_sibling_keys(isolated_env):
    write(
        isolated_env / "ao.toml",
        '[backends.local]\ntype = "openai_compat"\nbase_url = "http://a:1/v1"\nmodel = "m"\n',
    )
    write(isolated_env / "ao.local.toml", '[backends.local]\nbase_url = "http://b:2/v1"\n')

    backend = load_config().config.backends["local"]
    assert backend.base_url == "http://b:2/v1"
    assert backend.model == "m"


def test_unknown_key_error_points_at_dotted_path_and_file(isolated_env):
    write(
        isolated_env / "ao.local.toml", '[backends.local]\ntype = "openai_compat"\nbase_ulr = "x"\n'
    )

    with pytest.raises(ConfigError) as exc:
        load_config()
    message = str(exc.value)
    assert "backends.local.base_ulr" in message
    assert "ao.local.toml" in message


def test_invalid_toml_names_file(isolated_env):
    write(isolated_env / "ao.toml", "[paths\n")
    with pytest.raises(ConfigError, match="ao.toml: invalid TOML"):
        load_config()


def test_secret_ref_must_be_a_name_not_a_value(isolated_env):
    write(
        isolated_env / "ao.toml",
        '[backends.x]\ntype = "openai_compat"\napi_key_secret = "sk live key with spaces"\n',
    )
    with pytest.raises(ConfigError, match="backends.x.api_key_secret"):
        load_config()


def test_find_project_root_walks_up(isolated_env):
    nested = isolated_env / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == isolated_env


def test_data_dir_precedence(isolated_env, monkeypatch, tmp_path):
    write(isolated_env / "ao.toml", '[paths]\ndata_dir = "state"\n')
    assert load_config().data.root == isolated_env / "state"

    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "override"))
    assert load_config().data.root == tmp_path / "override"
    assert load_config().data.db_path == tmp_path / "override" / "db" / "ao.sqlite3"


def test_redact_masks_sensitive_keys_but_not_secret_refs():
    data = {
        "api_key": "sk-123",
        "nested": {"token": "t", "model": "m"},
        "api_key_secret": "llama",
        "empty_password": None,
    }
    assert redact(data) == {
        "api_key": REDACTED,
        "nested": {"token": REDACTED, "model": "m"},
        "api_key_secret": "llama",
        "empty_password": None,
    }


def test_secret_lookup_precedence(isolated_env, monkeypatch):
    write(isolated_env / ".env", "AO_SECRET_LLAMA=from-dotenv\nOTHER=x\n")
    secrets.load_dotenv_file(isolated_env)
    assert secrets.get_secret("llama") == "from-dotenv"
    assert secrets.secret_source("llama") == ".env"

    monkeypatch.setenv("AO_SECRET_LLAMA", "from-env")
    assert secrets.get_secret("llama") == "from-env"
    assert secrets.secret_source("llama") == "env"


def test_dotenv_is_not_exported_to_process_env(isolated_env):
    write(isolated_env / ".env", "AO_SECRET_LLAMA=from-dotenv\n")
    secrets.load_dotenv_file(isolated_env)
    assert "AO_SECRET_LLAMA" not in os.environ


def test_secret_keyring_fallback_and_missing(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring_get", lambda name: "kr" if name == "gh" else None)
    assert secrets.get_secret("gh") == "kr"
    assert secrets.secret_source("gh") == "keyring"
    with pytest.raises(secrets.SecretNotFound, match="AO_SECRET_NOPE"):
        secrets.get_secret("nope")


def test_env_var_name_normalises():
    assert secrets.env_var_name("my-api.key") == "AO_SECRET_MY_API_KEY"


def test_cli_config_show_never_prints_secret_value(isolated_env, monkeypatch):
    example = Path(__file__).parents[1] / "ao.example.toml"
    write(isolated_env / "ao.local.toml", example.read_text())
    monkeypatch.setenv("AO_SECRET_LLAMA", "super-secret-value")

    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert "super-secret-value" not in result.output
    payload = json.loads(result.output)
    assert payload["config"]["backends"]["local"]["is_local"] is True
    assert payload["secrets"] == {"llama": "env"}


def test_cli_config_show_reports_errors(isolated_env):
    write(isolated_env / "ao.toml", "[nope]\n")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 2
    assert "nope" in result.output


def test_cli_config_paths(isolated_env):
    result = runner.invoke(app, ["config", "paths"])
    assert result.exit_code == 0
    assert "ao.sqlite3" in result.output
