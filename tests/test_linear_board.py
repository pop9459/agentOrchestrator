import pytest
from typer.testing import CliRunner

from ao import memory
from ao.agents import create_agent
from ao.backends.base import Result, Usage
from ao.cli import app
from ao.config import load_config
from ao.db import store
from ao.linear import board, changes
from ao.linear.sync import sync
from ao.run import prepare
from fakes import FakeBackend
from linear_fake import FakeLinear, issue_node

cli = CliRunner()


def structured(data):
    return Result(
        outcome="ok",
        text="{}",
        model="fake",
        structured={**data, "memory": []},
        usage=Usage(input_tokens=40, output_tokens=20),
    )


@pytest.fixture
def env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    (isolated_env / "ao.local.toml").write_text('[linear]\nwrite_projects = ["ao sandbox"]\n')
    loaded = load_config()
    create_agent(loaded, "linear-manager")
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    fake_linear = FakeLinear(issues=[issue_node(1), issue_node(2, project="p-sandbox")])
    sync(conn, fake_linear.client(), "KAP")
    backend = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: backend)
    return loaded, conn, backend, fake_linear


def test_board_context_is_compact_and_complete(env):
    _, conn, _, _ = env
    text = board.board_context(conn, ["ao sandbox"])
    assert "States: Backlog (backlog), Todo (unstarted), In Progress (started), Done" in text
    assert "- Upload: Blackboard hand-in; use the file-naming rule" in text
    assert "- Create paperclip clone project (M4 Linear integration)" in text
    assert "Writable projects (new issues may only go here): ao sandbox" in text
    assert "OtherTeam" not in text
    assert len(text) < board.MAX_BOARD_CHARS


def test_provider_injects_board_into_every_run(env):
    loaded, _, _, _ = env
    prepared = prepare(loaded, "linear-manager", "anything")
    assert "## Board (Linear team metadata)" in prepared.request.system_prompt
    assert prepared.request.json_schema is not None  # writeback on
    # explicit section from the caller is not duplicated
    twice = prepare(loaded, "linear-manager", "x", extra_system=[("board", "## Board X")])
    assert twice.request.system_prompt.count("## Board") == 1


def test_draft_becomes_pending_change(env):
    loaded, conn, backend, fake_linear = env
    backend.results.append(
        structured(
            {
                "title": "Add TUI dashboard",
                "description": "Context\n\nAcceptance\n- [ ] shows agents",
                "project": "ao sandbox",
                "milestone": "",
                "labels": ["Feature", "Invented"],
                "priority": 3,
                "reasoning": "fits",
            }
        )
    )
    result = board.draft(conn, loaded, "a TUI dashboard")
    assert result.change.status == "pending" and result.change.created_by == "linear-manager"
    assert result.change.payload["labels"] == ["Feature"]
    assert result.warnings == ["dropped unknown label 'Invented'"]
    assert backend.requests[0].json_schema["required"][-1] == "memory"
    assert fake_linear.mutations() == []


def test_draft_outside_allowlist_is_not_proposable(env):
    loaded, conn, backend, _ = env
    backend.results.append(
        structured(
            {
                "title": "x",
                "description": "y",
                "project": "Create paperclip clone project",
                "milestone": "M9",
                "labels": [],
                "priority": 2,
                "reasoning": "",
            }
        )
    )
    result = board.draft(conn, loaded, "idea")
    assert result.change is None and "not in [linear] write_projects" in result.error
    assert any("dropped milestone" in w for w in result.warnings)
    assert changes.list_changes(conn) == []


def test_review_reads_and_proposes_within_allowlist(env):
    loaded, conn, backend, _ = env
    backend.results.append(
        structured(
            {
                "summary": "Two open issues.",
                "stale": ["KAP-1"],
                "missing_info": [{"identifier": "KAP-2", "what": "acceptance criteria"}],
                "suggestions": [
                    {
                        "identifier": "KAP-2",
                        "field": "priority",
                        "value": "2",
                        "reason": "important",
                    },
                    {
                        "identifier": "KAP-1",
                        "field": "state",
                        "value": "Done",
                        "reason": "finished",
                    },
                    {
                        "identifier": "KAP-2",
                        "field": "labels",
                        "value": "Feature, Upload",
                        "reason": "r",
                    },
                    {"identifier": "KAP-2", "field": "priority", "value": "high", "reason": "bad"},
                ],
            }
        )
    )
    result = board.review(conn, loaded, propose=True)
    prompt = backend.requests[0].prompt
    assert "KAP-1 | Todo | Feature |" in prompt and "description, no acceptance" in prompt
    assert [c.payload for c in result.proposed] == [
        {"priority": 2},
        {"labels": ["Feature", "Upload"]},
    ]
    assert any("KAP-1" in s and "refused" in s for s in result.skipped)
    assert any("unusable" in s for s in result.skipped)


def test_review_without_propose_writes_nothing(env):
    loaded, conn, backend, _ = env
    backend.results.append(
        structured(
            {
                "summary": "ok",
                "stale": [],
                "missing_info": [],
                "suggestions": [
                    {"identifier": "KAP-2", "field": "priority", "value": "1", "reason": "r"}
                ],
            }
        )
    )
    board.review(conn, loaded)
    assert changes.list_changes(conn) == []


def test_seed_memory_is_idempotent(env):
    loaded, conn, _, _ = env
    first = board.seed_memory(conn, loaded)
    facts = memory.load(loaded.agents_dir / "linear-manager")
    ids = [f.id for f in facts]
    assert "label-upload" in ids and "issue-format" in ids and "label-feature" not in ids
    assert len(first.applied) == len(ids)
    board.seed_memory(conn, loaded)
    assert memory.load(loaded.agents_dir / "linear-manager") == facts


def test_cli_draft_review_seed(env):
    loaded, conn, backend, _ = env
    backend.results.append(
        structured(
            {
                "title": "T",
                "description": "D",
                "project": "ao sandbox",
                "milestone": "",
                "labels": [],
                "priority": 4,
                "reasoning": "R",
            }
        )
    )
    drafted = cli.invoke(app, ["linear", "draft", "an idea"])
    assert drafted.exit_code == 0, drafted.output
    assert "# reasoning: R" in drafted.stdout and "ao linear apply 1" in drafted.stdout
    backend.results.append(
        structured({"summary": "S", "stale": ["KAP-1"], "missing_info": [], "suggestions": []})
    )
    reviewed = cli.invoke(app, ["linear", "review"])
    assert "# reviewed 2 open issues" in reviewed.stdout and "stale: KAP-1" in reviewed.stdout
    seeded = cli.invoke(app, ["linear", "seed-memory"])
    assert seeded.exit_code == 0 and "facts written" in seeded.stdout


def test_description_status():
    assert board._description_status(None) == "NO description"
    assert board._description_status("ctx\n**Acceptance**\n- x") == "description + acceptance"
    assert board._description_status("just text") == "description, no acceptance"
