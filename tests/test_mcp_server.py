import json
import os
import shutil

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ao import memory
from ao.agents import create_agent
from ao.config import load_config
from ao.db import repo, store
from ao.mcp_server import WITHHELD, Tools, build_server
from fakes import FakeBackend


@pytest.fixture
def env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path / "data"))
    loaded = load_config()
    create_agent(loaded, "idea-ingestor")
    directory = create_agent(loaded, "vault", "blank")
    (directory / "agent.toml").write_text('role = "secret keeper"\nclearance = "confidential"\n')
    fake = FakeBackend()
    monkeypatch.setattr("ao.run.get_backend", lambda name, config: fake)
    return loaded, Tools(), fake


def test_list_agents(env):
    _, tools, _ = env
    names = {a["name"]: a for a in tools.list_agents()}
    assert names["idea-ingestor"]["status"] == "ok"
    assert names["vault"]["clearance"] == "confidential"


def test_create_task_and_status(env):
    _, tools, fake = env
    created = tools.create_task("Look at this", "details", "idea-ingestor")
    assert created["status"] == "queued" and created["result"] is None
    assert tools.task_status(created["id"])["children"] == []
    assert fake.requests == []  # create does not run
    with pytest.raises(ValueError, match="not found"):
        tools.task_status(999)


def test_delegate_runs_through_runner(env):
    _, tools, fake = env
    done = tools.delegate("idea-ingestor", "Catalogue: offline mode")
    assert done["status"] == "done"
    assert done["result"] == "echo: Catalogue: offline mode\n\nCatalogue: offline mode"
    assert fake.requests[0].agent.name == "idea-ingestor"


def test_confidential_results_are_withheld(env):
    loaded, tools, fake = env
    fake.local = True  # vault may run (local backend), but its output must not leak via MCP
    outcome = tools.delegate("vault", "summarise my diary")
    assert outcome["status"] == "done" and outcome["result"] == WITHHELD
    conn = store.connect(loaded.data.db_path)
    task = repo.add_task(conn, "secret", agent="idea-ingestor", classification="confidential")
    repo.finish_task(conn, task.id, "done", result="raw secret text")
    assert tools.task_status(task.id)["result"] == WITHHELD
    assert all("raw secret" not in hit["text"] for hit in tools.search_context("secret"))


def test_search_context_tasks_and_memory(env):
    loaded, tools, _ = env
    tools.delegate("idea-ingestor", "offline mode idea")
    memory.save(loaded.agents_dir / "idea-ingestor", [memory.Fact("offline", "offline is key")])
    memory.save(loaded.agents_dir / "vault", [memory.Fact("offline", "vault secret offline")])
    hits = tools.search_context("offline")
    sources = [h["source"] for h in hits]
    assert sources[0].startswith("task #") and "memory idea-ingestor" in sources
    assert "memory vault" not in sources


def test_write_memory_validates(env):
    loaded, tools, _ = env
    assert tools.write_memory("idea-ingestor", "Tone", "brief")["id"] == "tone"
    assert memory.load(loaded.agents_dir / "idea-ingestor") == [memory.Fact("tone", "brief")]
    with pytest.raises(ValueError, match="rejected"):
        tools.write_memory("idea-ingestor", "!!!", "x")
    with pytest.raises(ValueError, match="not found"):
        tools.write_memory("ghost", "a", "b")


def test_in_memory_client_lists_and_calls(env):
    async def scenario():
        async with Client(build_server()) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            created = await client.call_tool("create_task", {"title": "via mcp"})
            return names, created

    names, created = anyio.run(scenario)
    assert names == [
        "create_task",
        "delegate",
        "list_agents",
        "search_context",
        "task_status",
        "write_memory",
    ]
    assert "via mcp" in json.dumps(created.model_dump(mode="json"))


@pytest.mark.skipif(shutil.which("ao") is None, reason="needs the installed ao entry point")
def test_stdio_round_trip(env, isolated_env):
    ao_bin = shutil.which("ao")
    params = StdioServerParameters(
        command=ao_bin,
        args=["mcp", "serve"],
        cwd=str(isolated_env),
        env={
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AO_DATA_DIR")
        },
    )

    async def scenario():
        async with Client(params) as client:
            tools = await client.list_tools()
            created = await client.call_tool("create_task", {"title": "stdio task"})
            return [t.name for t in tools.tools], created

    names, created = anyio.run(scenario)
    assert "delegate" in names
    assert "stdio task" in json.dumps(created.model_dump(mode="json"))
    loaded = load_config()
    conn = store.connect(loaded.data.db_path)
    assert [t.title for t in repo.list_tasks(conn)] == ["stdio task"]
