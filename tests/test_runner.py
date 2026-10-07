import pytest
from typer.testing import CliRunner

from ao import runner
from ao.agents import create_agent
from ao.backends.base import Result, Usage
from ao.cli import app
from ao.config import load_config
from ao.db import repo, store
from fakes import FakeBackend

cli = CliRunner()


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: backend)
    return backend


@pytest.fixture
def env(isolated_env, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    create_agent(loaded, "scout", "blank")
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    return loaded, conn


def kinds(conn):
    return [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")]


def test_task_prompt():
    task = repo.Task(1, "Title", "", None, "queued", None, "public", None, "", "")
    assert runner.task_prompt(task) == "Title"
    assert (
        runner.task_prompt(task.__class__(**{**task.__dict__, "body": "More"})) == "Title\n\nMore"
    )


def test_run_task_done_with_result_and_run_row(env, fake):
    loaded, conn = env
    task = repo.add_task(conn, "Sort ideas", "details", agent="scout")
    outcome = runner.run_task(conn, loaded, task.id)
    assert outcome.task.status == "done"
    assert outcome.task.result == "echo: Sort ideas\n\ndetails"
    assert outcome.task.attempts == 1
    assert [r["task_id"] for r in repo.runs_for_task(conn, task.id)] == [task.id]
    assert kinds(conn)[-2:] == ["run.finished", "task.done"]


def test_failure_and_refusal_statuses(env, fake):
    loaded, conn = env
    fake.results.append(Result(outcome="timeout", error="slow", usage=Usage()))
    failed = runner.run_task(conn, loaded, repo.add_task(conn, "a", agent="scout").id)
    assert (failed.task.status, failed.task.error) == ("failed", "slow")

    (loaded.agents_dir / "scout" / "agent.toml").write_text(
        'role = "r"\n[budget]\ndaily_tokens = 1\n'
    )
    runner.run_task(conn, loaded, repo.add_task(conn, "b", agent="scout").id)  # uses budget
    waiting = runner.run_task(conn, loaded, repo.add_task(conn, "c", agent="scout").id)
    assert waiting.task.status == "waiting"
    assert "daily tokens" in waiting.task.error


def test_not_runnable_and_unassigned(env, fake):
    loaded, conn = env
    with pytest.raises(runner.TaskError, match="no agent"):
        runner.run_task(conn, loaded, repo.add_task(conn, "x").id)
    done = runner.run_task(conn, loaded, repo.add_task(conn, "y", agent="scout").id).task
    with pytest.raises(runner.TaskError, match="is done"):
        runner.run_task(conn, loaded, done.id)


def test_unknown_agent_fails_task(env, fake):
    loaded, conn = env
    outcome = runner.run_task(conn, loaded, repo.add_task(conn, "x", agent="ghost").id)
    assert outcome.task.status == "failed"
    assert "not found" in outcome.task.error


def test_loop_guard(env, fake):
    loaded, conn = env
    task = repo.add_task(conn, "flaky", agent="scout")
    for _ in range(runner.MAX_ATTEMPTS):
        fake.results.append(Result(outcome="error", error="nope", usage=Usage()))
        runner.run_task(conn, loaded, task.id)
        conn.execute("UPDATE tasks SET status = 'queued' WHERE id = ?", (task.id,))
    guarded = runner.run_task(conn, loaded, task.id)
    assert guarded.task.status == "failed"
    assert "loop guard" in guarded.task.error
    assert len(fake.requests) == runner.MAX_ATTEMPTS
    assert "task.loop_guard" in kinds(conn)

    repo.reset_task(conn, task.id)
    assert runner.run_task(conn, loaded, task.id).task.status == "done"


def test_confidential_task_never_reaches_cloud_backend(env, fake):
    loaded, conn = env
    task = repo.add_task(conn, "my mail", agent="scout", classification="confidential")
    outcome = runner.run_task(conn, loaded, task.id)
    assert outcome.task.status == "failed"
    assert "not local" in outcome.task.error
    assert fake.requests == []
    assert "routing.refused" in kinds(conn)

    fake.local = True
    repo.reset_task(conn, task.id)
    assert runner.run_task(conn, loaded, task.id).task.status == "done"


def test_run_all_drains_in_order(env, fake):
    loaded, conn = env
    ids = [repo.add_task(conn, f"t{i}", agent="scout").id for i in range(3)]
    outcomes = runner.run_all(conn, loaded)
    assert [o.task.id for o in outcomes] == ids
    assert [r.prompt for r in fake.requests] == ["t0", "t1", "t2"]
    assert runner.run_next(conn, loaded) is None


def test_cli_task_flow(env, fake):
    loaded, conn = env
    assert (
        cli.invoke(app, ["task", "add", "Parent idea", "--agent", "scout"]).stdout == "queued #1\n"
    )
    assert (
        cli.invoke(app, ["task", "add", "Child", "--agent", "scout", "--parent", "1"]).exit_code
        == 0
    )
    cli.invoke(app, ["task", "add", "Body via stdin", "--agent", "scout", "--body", "-"],
               input="the body")  # fmt: skip
    assert repo.get_task(conn, 3).body == "the body"
    assert repo.get_task(conn, 2).depth == 1

    tree = cli.invoke(app, ["task", "list", "--tree"]).stdout.splitlines()
    assert tree[0].startswith("· #1") and tree[1].startswith("  · #2")

    result = cli.invoke(app, ["task", "run", "--next"])
    assert result.exit_code == 0, result.output
    assert "echo: Parent idea" in result.stdout

    assert cli.invoke(app, ["task", "run", "--all"]).exit_code == 0
    shown = cli.invoke(app, ["task", "show", "1"]).stdout
    assert "--- result ---" in shown and "run #1: ok" in shown and "child: ✓ #2" in shown
    assert cli.invoke(app, ["task", "run"]).exit_code == 2
    assert cli.invoke(app, ["task", "run", "1"]).exit_code == 2  # already done
    assert cli.invoke(app, ["task", "list", "--status", "bogus"]).exit_code == 2


def test_cli_run_failure_exit_and_retry(env, fake):
    loaded, conn = env
    cli.invoke(app, ["task", "add", "x", "--agent", "scout"])
    fake.results.append(Result(outcome="error", error="boom", usage=Usage()))
    result = cli.invoke(app, ["task", "run", "1"])
    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert cli.invoke(app, ["task", "retry", "1"]).stdout == "re-queued #1\n"
    assert cli.invoke(app, ["task", "run", "1"]).exit_code == 0
