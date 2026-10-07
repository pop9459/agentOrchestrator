import pytest
from typer.testing import CliRunner

from ao import jarvis
from ao.agents import create_agent
from ao.backends.base import Result, RunRequest, Usage
from ao.cli import app
from ao.config import load_config
from ao.db import repo, store
from fakes import FakeBackend

cli = CliRunner()


def structured(reply, delegations=(), session="sess-1"):
    return Result(outcome="ok", text="{}", model="fake", session_id=session,
                  usage=Usage(input_tokens=50, output_tokens=5),
                  structured={"reply": reply, "delegations": list(delegations)})  # fmt: skip


class Script:
    """Per-agent scripted answers; Jarvis answers come from a queue."""

    def __init__(self, jarvis_answers):
        self.jarvis_answers = list(jarvis_answers)
        self.seen: list[RunRequest] = []

    def __call__(self, request: RunRequest) -> Result:
        self.seen.append(request)
        if request.agent.name == "jarvis":
            return self.jarvis_answers.pop(0)
        text = f"{request.agent.name} did: {request.prompt}"
        structured = {"reply": text, "memory": []} if request.json_schema else None
        return Result(outcome="ok", text=text, structured=structured, model="fake",
                      usage=Usage(input_tokens=20, output_tokens=3))  # fmt: skip

    def jarvis_requests(self):
        return [r for r in self.seen if r.agent.name == "jarvis"]


@pytest.fixture
def env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    for name in ("jarvis", "idea-ingestor", "linear-manager", "local"):
        create_agent(loaded, name)
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    fake = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: fake)
    return loaded, conn, fake


def test_roster_lists_runnable_delegable_agents(env):
    loaded, _, _ = env
    names = [n for n, _ in jarvis.roster(loaded)]
    assert names == ["idea-ingestor", "linear-manager"]  # no jarvis, local not configured
    section = jarvis.roster_section(jarvis.roster(loaded))
    assert (
        "idea-ingestor: Turns raw head-dump ideas" in section and "(claude/haiku, cloud" in section
    )


def test_direct_answer(env):
    loaded, conn, fake = env
    fake.responder = Script([structured("Hello Peter.")])
    turn = jarvis.handle(conn, loaded, "hi")
    assert (turn.reply, turn.error, turn.delegated) == ("Hello Peter.", None, [])
    assert repo.get_task(conn, turn.task_id).status == "done"
    req = fake.responder.jarvis_requests()[0]
    assert req.json_schema == jarvis.SCHEMA
    assert (
        "## Agents you can delegate to" in req.system_prompt
        and "## How to respond" in req.system_prompt
    )
    assert req.persist_session is False and req.resume_session_id is None


def test_delegates_to_two_agents_and_summarises(env):
    """M2 exit criterion."""
    loaded, conn, fake = env
    delegations = [
        {"agent": "idea-ingestor", "title": "Catalogue TUI", "task": "Catalogue: TUI dashboard"},
        {"agent": "linear-manager", "title": "Draft issue", "task": "Draft issue: TUI dashboard"},
    ]
    script = Script([structured("On it.", delegations), structured("Catalogued and drafted.")])
    fake.responder = script
    events = []
    turn = jarvis.handle(conn, loaded, "Catalogue 'TUI dashboard' and draft an issue",
                         on_event=lambda kind, data: events.append(kind))  # fmt: skip

    assert turn.reply == "Catalogued and drafted."
    assert [o.task.status for o in turn.delegated] == ["done", "done"]
    children = repo.child_tasks(conn, turn.task_id)
    assert [(c.agent, c.depth) for c in children] == [("idea-ingestor", 1), ("linear-manager", 1)]
    assert events == ["delegate", "result", "delegate", "result"]
    follow_up = script.jarvis_requests()[1].prompt
    assert "Original request:" in follow_up  # no session → request repeated
    assert "idea-ingestor did: Catalogue TUI\n\nCatalogue: TUI dashboard" in follow_up
    usage = jarvis.turn_usage(conn, turn.task_id)
    assert usage["runs"] == 4 and usage["tokens_in"] == 50 + 20 + 20 + 50


def test_unknown_agents_and_limits_are_reported_back(env):
    loaded, conn, fake = env
    many = [{"agent": "idea-ingestor", "title": f"t{i}", "task": f"x{i}"} for i in range(5)]
    script = Script([
        structured("…", [{"agent": "ghost", "title": "x", "task": "y"},
                         {"agent": "jarvis", "title": "x", "task": "y"}, *many]),
        structured("done"),
    ])  # fmt: skip
    fake.responder = script
    turn = jarvis.handle(conn, loaded, "go")
    assert len(turn.delegated) == jarvis.MAX_DELEGATIONS
    follow_up = script.jarvis_requests()[1].prompt
    assert "agent 'ghost' is not available" in follow_up
    assert "agent 'jarvis' is not available" in follow_up
    assert "over the limit" in follow_up


def test_round_cap_forces_final_answer(env):
    loaded, conn, fake = env
    again = [{"agent": "idea-ingestor", "title": "t", "task": "x"}]
    script = Script([structured("r1", again), structured("r2", again), structured("r3", again)])
    fake.responder = script
    turn = jarvis.handle(conn, loaded, "loop forever")
    assert turn.reply == "r3"
    assert len(turn.delegated) == jarvis.MAX_ROUNDS
    assert len(script.jarvis_requests()) == jarvis.MAX_ROUNDS + 1
    assert "do not delegate again" in script.jarvis_requests()[-1].prompt


def test_session_threading(env):
    loaded, conn, fake = env
    script = Script([structured("…", [{"agent": "idea-ingestor", "title": "t", "task": "x"}],
                                session="S1"), structured("done", session="S1"),
                     structured("second turn", session="S1")])  # fmt: skip
    fake.responder = script
    first = jarvis.handle(conn, loaded, "one", persist_session=True)
    assert first.session_id == "S1"
    second = jarvis.handle(conn, loaded, "two", persist_session=True, session_id=first.session_id)
    reqs = script.jarvis_requests()
    assert [(r.persist_session, r.resume_session_id) for r in reqs] == [
        (True, None), (True, "S1"), (True, "S1")]  # fmt: skip
    assert "Original request:" not in reqs[1].prompt  # session carries it
    assert second.reply == "second turn"


def test_jarvis_failure_fails_parent(env):
    loaded, conn, fake = env
    fake.responder = lambda request: Result(outcome="error", error="boom", usage=Usage())
    turn = jarvis.handle(conn, loaded, "hi")
    assert turn.error == "boom"
    assert repo.get_task(conn, turn.task_id).status == "failed"


def test_cli_ask_and_chat(env):
    loaded, conn, fake = env
    fake.responder = Script([
        structured("…", [{"agent": "idea-ingestor", "title": "Cat", "task": "x"}]),
        structured("All done."),
        structured("chat 1", session="C1"),
        structured("chat 2", session="C1"),
    ])  # fmt: skip
    result = cli.invoke(app, ["ask", "do it"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "All done."
    assert "→ #2 idea-ingestor: Cat" in result.stderr and "[3 runs" in result.stderr

    chat = cli.invoke(app, ["chat"], input="hello\nagain\n/usage\n/quit\n")
    assert chat.exit_code == 0, chat.output
    assert "chat 1" in chat.stdout and "chat 2" in chat.stdout
    assert "2 turns" in chat.stderr
    assert (loaded.data.root / "chat" / "jarvis.session").read_text() == "C1"
    assert fake.responder.jarvis_requests()[-1].resume_session_id == "C1"
