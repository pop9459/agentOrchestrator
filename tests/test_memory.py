import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ao import compaction, memory
from ao.agents import create_agent
from ao.backends.base import Result, RunRequest, Usage
from ao.backends.claude_code import build_argv, parse_result
from ao.cli import app
from ao.config import BackendConfig, load_config
from ao.db import store
from ao.run import execute, prepare
from fakes import FakeBackend

FIXTURE = Path(__file__).parent / "fixtures" / "claude_result_structured.json"
cli = CliRunner()


def ok(structured, text="{}"):
    return Result(outcome="ok", text=text, structured=structured, model="fake",
                  usage=Usage(input_tokens=10, output_tokens=5))  # fmt: skip


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: backend)
    return backend


@pytest.fixture
def env(isolated_env, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    directory = create_agent(loaded, "scout", "blank")
    (directory / "agent.toml").write_text('role = "r"\n[memory]\nwriteback = true\nmax_facts = 3\n')
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    return loaded, conn, directory


# --- pure memory functions ------------------------------------------------------------------


def test_load_render_roundtrip_and_hand_written_lines(tmp_path):
    path = memory.index_path(tmp_path)
    path.parent.mkdir()
    path.write_text("# heading\n- [Tone] likes  brief\n   answers\nplain note\n- [tone] dup\n")
    facts = memory.load(tmp_path)
    assert facts == [memory.Fact("tone", "likes brief"), memory.Fact("note-3", "answers"),
                     memory.Fact("note-4", "plain note")]  # fmt: skip
    memory.save(tmp_path, facts)
    assert memory.load(tmp_path) == facts


def test_apply_ops_and_rejections():
    facts = [memory.Fact("a", "one")]
    out = memory.apply(facts, [
        {"op": "add", "id": "B Fact", "text": "two\nlines"},
        {"op": "update", "id": "a", "text": "uno"},
        {"op": "delete", "id": "ghost"},
        {"op": "add", "id": "!!!", "text": "x"},
        {"op": "add", "id": "c", "text": ""},
        {"op": "explode", "id": "a"},
        "garbage",
        {"op": "add", "id": "d", "text": "x" * 500},
        {"op": "add", "id": "e", "text": "over limit"},
    ], max_facts=3)  # fmt: skip
    assert [f.id for f in out.facts] == ["a", "b-fact", "d"]
    assert out.facts[0].text == "uno" and out.facts[1].text == "two lines"
    assert len(out.facts[2].text) == memory.MAX_TEXT
    reasons = [r for _, r in out.rejected]
    assert reasons == ["unknown id", "invalid id", "empty text", "invalid op", "invalid op",
                       "memory full (3 facts); compact it"]  # fmt: skip


# --- backend: structured output ----------------------------------------------------------------


def test_parse_real_structured_output():
    result = parse_result(FIXTURE.read_text(), 0)
    assert result.outcome == "ok"
    assert result.structured["memory"][0]["op"] == "add"
    assert "bullet" in result.structured["reply"]


def test_schema_forces_at_least_two_turns(env):
    loaded, _, _ = env
    prepared = prepare(loaded, "scout", "hi")
    (loaded.agents_dir / "scout" / "agent.toml").write_text('role = "r"\n[limits]\nmax_turns = 1\n')
    one_turn = prepare(loaded, "scout", "hi", output_schema={"type": "object"})
    argv = build_argv(BackendConfig(type="claude_code"), one_turn.request)
    assert argv[argv.index("--max-turns") + 1] == "2"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert prepared.request.json_schema["required"] == ["reply", "memory"]


# --- end to end through run.execute ----------------------------------------------------------


def test_writeback_adds_protocol_and_applies_ops(env, fake):
    loaded, conn, directory = env
    fake.results.append(ok({"reply": "Noted!", "memory": [
        {"op": "add", "id": "format", "text": "User prefers bullet points"}]}))  # fmt: skip
    prepared = prepare(loaded, "scout", "I like bullet points")
    assert "## Memory protocol" in prepared.request.system_prompt
    record = execute(conn, prepared)
    assert record.result.text == "Noted!"
    assert memory.load(directory) == [memory.Fact("format", "User prefers bullet points")]
    assert conn.execute("SELECT kind FROM events WHERE kind LIKE 'memory.%'").fetchone()[0] == (
        "memory.add"
    )
    # next run sees the fact in its system prompt
    assert (
        "- [format] User prefers bullet points"
        in prepare(loaded, "scout", "x").request.system_prompt
    )


def test_missing_structured_output_is_an_error(env, fake):
    loaded, conn, directory = env
    fake.results.append(Result(outcome="ok", text="plain text", usage=Usage()))
    record = execute(conn, prepare(loaded, "scout", "hi"))
    assert record.result.outcome == "error"
    assert "structured output" in record.result.error
    assert memory.load(directory) == []


def test_rejected_ops_are_logged_not_applied(env, fake):
    loaded, conn, directory = env
    fake.results.append(ok({"reply": "ok", "memory": [{"op": "delete", "id": "nope"}]}))
    execute(conn, prepare(loaded, "scout", "hi"))
    reason = conn.execute("SELECT json_extract(data, '$.reason') FROM events"
                          " WHERE kind = 'memory.rejected'").fetchone()[0]  # fmt: skip
    assert reason == "unknown id"


def test_no_writeback_means_no_schema(env, fake):
    loaded, conn, _ = env
    (loaded.agents_dir / "scout" / "agent.toml").write_text('role = "r"\n')
    prepared = prepare(loaded, "scout", "hi")
    assert prepared.request.json_schema is None
    assert "Memory protocol" not in prepared.request.system_prompt


# --- compaction and CLI ------------------------------------------------------------------------


def test_compaction_proposal_and_cli(env, fake):
    loaded, conn, directory = env
    memory.save(directory, [memory.Fact("a", "likes tea"), memory.Fact("b", "likes tea a lot"),
                            memory.Fact("c", "old project X")])  # fmt: skip
    fake.results.append(
        ok({"facts": [{"id": "a", "text": "likes tea a lot"}, {"id": "!", "text": "x"}]})
    )
    proposal = compaction.propose(conn, loaded, "scout")
    req = fake.requests[-1]
    assert req.json_schema == compaction.SCHEMA and req.model == "haiku"
    assert (
        "maintain another agent's" in req.system_prompt
        and "Memory protocol" not in req.system_prompt
    )
    assert proposal.after == [memory.Fact("a", "likes tea a lot")]
    assert "- [c] old project X" in proposal.diff()

    fake.results.append(ok({"facts": [{"id": "a", "text": "likes tea a lot"}]}))
    declined = cli.invoke(app, ["memory", "compact", "scout"], input="n\n")
    assert "left unchanged" in declined.stdout and len(memory.load(directory)) == 3
    fake.results.append(ok({"facts": [{"id": "a", "text": "likes tea a lot"}]}))
    applied = cli.invoke(app, ["memory", "compact", "scout", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert memory.load(directory) == [memory.Fact("a", "likes tea a lot")]


def test_cli_show_add_rm(env):
    assert cli.invoke(app, ["memory", "add", "scout", "tone", "brief answers"]).exit_code == 0
    shown = cli.invoke(app, ["memory", "show", "scout"]).stdout
    assert "1/3 facts · writeback on" in shown and "- [tone] brief answers" in shown
    assert cli.invoke(app, ["memory", "rm", "scout", "ghost"]).exit_code == 2
    assert cli.invoke(app, ["memory", "rm", "scout", "tone"]).exit_code == 0
    assert "- [tone]" not in cli.invoke(app, ["memory", "show", "scout"]).stdout
    with pytest.raises(compaction.CompactionError):
        loaded, conn, _ = env
        compaction.propose(conn, loaded, "scout")


def test_request_type_unchanged():
    assert RunRequest.__dataclass_fields__["json_schema"].default is None
