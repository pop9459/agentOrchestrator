import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ao import secrets
from ao.agents import create_agent
from ao.cli import app
from ao.config import load_config
from ao.db import repo, store
from ao.linear import mirror
from ao.linear.client import LinearError, client_from_config
from ao.linear.sync import sync
from ao.mcp_server import Tools
from fakes import FakeBackend
from linear_fake import FakeLinear, issue_node

cli = CliRunner()


@pytest.fixture
def env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AO_SECRET_LINEAR", "lin_test_key")
    loaded = load_config()
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    fake = FakeLinear(issues=[issue_node(n) for n in (1, 2, 3)])
    monkeypatch.setattr("ao.cli_linear.client_from_config", lambda loaded: fake.client())
    return loaded, conn, fake


def test_client_needs_api_key(isolated_env, monkeypatch):
    with pytest.raises(LinearError, match="AO_SECRET_LINEAR"):
        client_from_config(load_config())
    monkeypatch.setenv("AO_SECRET_LINEAR", "k")
    assert client_from_config(load_config()) is not None


def test_client_errors(env):
    _, _, fake = env
    fake.status = 401
    with pytest.raises(LinearError, match="rejected the API key"):
        fake.client().team("KAP")
    fake.status = 429
    with pytest.raises(LinearError, match="rate limit"):
        fake.client().team("KAP")
    fake.status = 200
    with pytest.raises(LinearError, match="no Linear team"):
        fake.client().team("NOPE")
    with pytest.raises(LinearError, match="unknown query"):
        fake.client().query("query { whatever }")


def test_full_sync_paginates_and_stores_metadata(env):
    _, conn, fake = env
    report = sync(conn, fake.client(), "KAP")
    assert (report.fetched, report.full) == (3, True)
    assert len([c for c in fake.calls if "issues(" in c["query"]]) == 2  # page size 2
    assert all(c["auth"] == "lin_test_key" for c in fake.calls)
    issue = mirror.get(conn, "kap-2")
    assert (issue.title, issue.state_type, issue.labels, issue.project_name) == (
        "Issue 2",
        "unstarted",
        ("Feature",),
        "Create paperclip clone project",
    )
    assert [lbl["name"] for lbl in mirror.get_meta(conn, "labels")] == ["Feature", "Upload"]
    assert mirror.get_meta(conn, "team")["key"] == "KAP"
    assert fake.mutations() == []  # read-only


def test_incremental_sync_and_idempotency(env):
    _, conn, fake = env
    sync(conn, fake.client(), "KAP")
    fake.calls.clear()
    fake.issues[0] = issue_node(1, state="Done", updated="2099-01-01T00:00:00.000Z")
    report = sync(conn, fake.client(), "KAP")
    issue_calls = [c for c in fake.calls if "issues(" in c["query"]]
    assert "updatedAt" in issue_calls[0]["variables"]["filter"]
    assert (report.fetched, report.full) == (1, False)
    assert mirror.get(conn, "KAP-1").state_type == "completed"
    assert conn.execute("SELECT count(*) FROM linear_issues").fetchone()[0] == 3


def test_full_sync_removes_deleted(env):
    _, conn, fake = env
    sync(conn, fake.client(), "KAP")
    fake.issues.pop()
    assert sync(conn, fake.client(), "KAP", full=True).deleted == 1
    assert mirror.get(conn, "KAP-3") is None


def test_search_filters(env):
    _, conn, fake = env
    fake.issues.append(issue_node(9, project="p-sandbox", state="Done"))
    sync(conn, fake.client(), "KAP")
    assert [i.identifier for i in mirror.search(conn, project="ao SANDBOX")] == ["KAP-9"]
    assert "KAP-9" not in [i.identifier for i in mirror.search(conn, open_only=True)]
    assert [i.identifier for i in mirror.search(conn, text="Body 2")] == ["KAP-2"]


def test_cli_sync_issues_show(env):
    result = cli.invoke(app, ["linear", "sync"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "synced KAP (full): 3 issues"
    listing = cli.invoke(app, ["linear", "issues", "--search", "Issue 1"]).stdout
    assert listing.startswith("KAP-1    [Todo] Issue 1 · Create paperclip clone project")
    shown = cli.invoke(app, ["linear", "show", "KAP-1"]).stdout
    assert "priority: medium · labels: Feature" in shown and "Body 1" in shown
    assert cli.invoke(app, ["linear", "show", "KAP-404"]).exit_code == 2


def test_cli_task_handoff_links_issue(env, monkeypatch):
    loaded, conn, _ = env
    cli.invoke(app, ["linear", "sync"])
    create_agent(loaded, "idea-ingestor")
    fake_backend = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: fake_backend)
    result = cli.invoke(app, ["linear", "task", "KAP-2", "--agent", "idea-ingestor", "--run"])
    assert result.exit_code == 0, result.output
    task = repo.get_task(conn, 1)
    assert (task.title, task.linear_issue_id, task.status) == ("KAP-2: Issue 2", "KAP-2", "done")
    assert fake_backend.requests[0].prompt == "KAP-2: Issue 2\n\nBody 2"


def test_mcp_search_includes_mirror(env):
    _, conn, fake = env
    sync(conn, fake.client(), "KAP")
    hits = Tools().search_context("Body 3")
    assert hits[0]["source"] == "linear KAP-3"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("AO_LIVE") != "1", reason="set AO_LIVE=1 (and AO_SECRET_LINEAR in .env)"
)
def test_live_read_only_sync(isolated_env, tmp_path, monkeypatch):
    repo_root = Path(__file__).parents[1]
    secrets.load_dotenv_file(repo_root)
    if secrets.secret_source("linear") is None:
        pytest.skip("no AO_SECRET_LINEAR")
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "live"))
    loaded = load_config()
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    report = sync(conn, client_from_config(loaded), "KAP")
    assert report.fetched > 0
    assert mirror.get(conn, "KAP-93").project_name == "Create paperclip clone project"
