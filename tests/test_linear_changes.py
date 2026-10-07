import pytest
from typer.testing import CliRunner

from ao.agents import create_agent
from ao.cli import app
from ao.config import load_config
from ao.db import store
from ao.linear import changes, mirror
from ao.linear.sync import sync
from fakes import FakeBackend
from linear_fake import FakeLinear, issue_node

cli = CliRunner()


@pytest.fixture
def env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AO_SECRET_LINEAR", "k")
    (isolated_env / "ao.local.toml").write_text('[linear]\nwrite_projects = ["ao sandbox"]\n')
    loaded = load_config()
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    fake = FakeLinear(issues=[issue_node(1), issue_node(2, project="p-sandbox")])
    sync(conn, fake.client(), "KAP")
    fake.calls.clear()
    monkeypatch.setattr("ao.cli_linear.client_from_config", lambda loaded: fake.client())
    return loaded, conn, fake


def test_propose_create_validates_and_sends_nothing(env):
    loaded, conn, fake = env
    change = changes.propose_create(
        conn,
        loaded,
        project="AO Sandbox",
        title="Try ao",
        description="body",
        labels=["feature"],
        priority=3,
    )
    assert (change.status, change.kind, change.project_id) == (
        "pending",
        "create_issue",
        "p-sandbox",
    )
    assert change.payload == {
        "title": "Try ao",
        "description": "body",
        "labels": ["feature"],
        "priority": 3,
    }
    assert fake.calls == []  # nothing sent before apply
    for kwargs, message in [
        ({"project": "nope"}, "unknown project"),
        ({"project": "ao sandbox", "labels": ["Nope"]}, "unknown label"),
        ({"project": "ao sandbox", "milestone": "M9"}, "unknown milestone"),
        ({"project": "ao sandbox", "priority": 9}, "priority"),
    ]:
        with pytest.raises(changes.ChangeError, match=message):
            changes.propose_create(conn, loaded, title="x", **kwargs)


def test_writes_outside_allowlist_are_refused_at_propose(env):
    loaded, conn, _ = env
    with pytest.raises(changes.ChangeError, match="not in \\[linear\\] write_projects"):
        changes.propose_create(conn, loaded, project="Create paperclip clone project", title="x")
    with pytest.raises(changes.ChangeError, match="update of KAP-1 refused"):
        changes.propose_update(conn, loaded, "KAP-1", {"title": "y"})
    with pytest.raises(changes.ChangeError, match="comment on KAP-1 refused"):
        changes.propose_comment(conn, loaded, "KAP-1", "hi")
    assert changes.list_changes(conn) == []


def test_apply_create_update_comment(env):
    loaded, conn, fake = env
    created = changes.apply(
        conn,
        loaded,
        fake.client(),
        changes.propose_create(
            conn, loaded, project="ao sandbox", title="New one", labels=["Upload"]
        ).id,
    )
    assert created.status == "applied" and created.result["identifier"].startswith("KAP-")
    create_call = fake.mutations()[0]["variables"]["input"]
    assert create_call == {
        "teamId": "team-1",
        "projectId": "p-sandbox",
        "title": "New one",
        "labelIds": ["l-upload"],
    }
    assert mirror.get(conn, created.result["identifier"]).title == "New one"

    update = changes.propose_update(conn, loaded, "KAP-2", {"title": "Renamed", "state": "Done"})
    assert "- title: Issue 2" in changes.render(conn, update)
    applied = changes.apply(conn, loaded, fake.client(), update.id)
    assert applied.status == "applied"
    assert fake.mutations()[-1]["variables"] == {
        "id": "uuid-2",
        "input": {"title": "Renamed", "stateId": "st-done"},
    }
    assert mirror.get(conn, "KAP-2").title == "Renamed"

    comment = changes.apply(
        conn, loaded, fake.client(), changes.propose_comment(conn, loaded, "KAP-2", "done!").id
    )
    assert comment.result["url"] == "https://linear.app/c-1"
    assert fake.mutations()[-1]["variables"]["input"] == {"issueId": "uuid-2", "body": "done!"}


def test_apply_rechecks_live_project(env):
    loaded, conn, fake = env
    change = changes.propose_comment(conn, loaded, "KAP-2", "hi")
    fake.issues[1]["project"] = {"id": "p-roadmap", "name": "Create paperclip clone project"}
    failed = changes.apply(conn, loaded, fake.client(), change.id)
    assert failed.status == "failed" and "issue moved" in failed.error
    assert fake.mutations() == []


def test_apply_rechecks_config(env, isolated_env):
    loaded, conn, fake = env
    change = changes.propose_create(conn, loaded, project="ao sandbox", title="x")
    (isolated_env / "ao.local.toml").write_text("[linear]\nwrite_projects = []\n")
    failed = changes.apply(conn, load_config(), fake.client(), change.id)
    assert failed.status == "failed" and fake.mutations() == []


def test_reject_and_double_decide(env):
    loaded, conn, fake = env
    change = changes.propose_comment(conn, loaded, "KAP-2", "hi")
    assert changes.reject(conn, change.id).status == "rejected"
    with pytest.raises(changes.ChangeError, match="rejected"):
        changes.apply(conn, loaded, fake.client(), change.id)
    with pytest.raises(changes.ChangeError, match="rejected"):
        changes.reject(conn, change.id)
    assert fake.mutations() == []


def test_api_failure_marks_failed(env):
    loaded, conn, fake = env
    change = changes.propose_comment(conn, loaded, "KAP-2", "hi")
    fake.status = 500
    assert changes.apply(conn, loaded, fake.client(), change.id).status == "failed"


def test_cli_flow(env):
    loaded, conn, fake = env
    out = cli.invoke(app, ["linear", "new", "--project", "ao sandbox", "--title", "From CLI"])
    assert out.exit_code == 0, out.output
    assert "proposed change #1" in out.stdout
    assert (
        "#1 [pending] create issue in 'ao sandbox': From CLI"
        in cli.invoke(app, ["linear", "pending"]).stdout
    )
    declined = cli.invoke(app, ["linear", "apply", "1"], input="n\n")
    assert "not applied" in declined.stdout and fake.mutations() == []
    applied = cli.invoke(app, ["linear", "apply", "1", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert "applied #1" in applied.stdout
    assert cli.invoke(app, ["linear", "apply", "1", "--yes"]).exit_code == 2
    refused = cli.invoke(app, ["linear", "comment", "KAP-1", "hello"])
    assert refused.exit_code == 2 and "refused" in refused.stderr
    cli.invoke(app, ["linear", "edit", "KAP-2", "--state", "Done"])
    assert cli.invoke(app, ["linear", "reject", "2"]).stdout == "rejected #2\n"
    assert cli.invoke(app, ["linear", "pending"]).stdout == "no pending changes\n"


def test_task_comment_back_proposes_only(env, monkeypatch):
    loaded, conn, fake = env
    create_agent(loaded, "idea-ingestor")
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: FakeBackend())
    result = cli.invoke(
        app, ["linear", "task", "KAP-2", "--agent", "idea-ingestor", "--run", "--comment-back"]
    )
    assert result.exit_code == 0, result.output
    (change,) = changes.list_changes(conn, "pending")
    assert change.kind == "comment" and change.created_by == "idea-ingestor"
    assert change.payload["body"].startswith("ao task #1 (idea-ingestor) finished:")
    assert fake.mutations() == []
    outside = cli.invoke(
        app, ["linear", "task", "KAP-1", "--agent", "idea-ingestor", "--run", "--comment-back"]
    )
    assert "comment-back not proposed" in outside.stderr
