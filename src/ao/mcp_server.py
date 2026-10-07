"""`ao mcp serve`: the orchestrator as an MCP server (stdio).

Meant for interactive Claude Code sessions (`claude mcp add ao -- uv --directory <repo>
run ao mcp serve`). Jarvis itself does not use it: tool mode costs ~4-18k tokens per call
versus ~1k for structured output (KAP-102/KAP-85).

Every tool goes through the same code paths as the CLI (runner, budgets, attempt guard,
confidentiality guard). Confidential data is never returned to the MCP client:
confidential tasks and confidential agents' results/memory are withheld.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ao import memory, runner
from ao.agents import AgentError, agent_problem, list_agent_names, load_agent
from ao.config import LoadedConfig, load_config
from ao.db import repo, store

WITHHELD = "(withheld: confidential; see KAP-91 airlock)"
MAX_RESULT_CHARS = 4000


class Tools:
    """Plain functions behind the MCP tools; one short-lived DB connection per call."""

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root

    def _open(self) -> tuple[LoadedConfig, sqlite3.Connection]:
        loaded = load_config(self.project_root)
        conn = store.connect(loaded.data.ensure().db_path)
        store.migrate(conn)
        return loaded, conn

    def _confidential_agent(self, loaded: LoadedConfig, name: str | None) -> bool:
        if not name:
            return False
        try:
            return load_agent(loaded, name).config.clearance == "confidential"
        except AgentError:
            return False

    def _task_view(self, loaded: LoadedConfig, task: repo.Task) -> dict[str, Any]:
        hidden = task.classification == "confidential" or self._confidential_agent(
            loaded, task.agent
        )
        result = task.result if not hidden else (WITHHELD if task.result else None)
        return {
            "id": task.id,
            "title": task.title,
            "agent": task.agent,
            "status": task.status,
            "parent_id": task.parent_id,
            "error": task.error,
            "result": result[:MAX_RESULT_CHARS] if result else None,
        }

    def list_agents(self) -> list[dict[str, Any]]:
        loaded, conn = self._open()
        conn.close()
        agents = []
        for name in list_agent_names(loaded):
            try:
                agent = load_agent(loaded, name)
            except AgentError as exc:
                agents.append({"name": name, "status": f"invalid: {exc}"})
                continue
            agents.append(
                {
                    "name": name,
                    "role": agent.config.role,
                    "backend": agent.config.backend,
                    "clearance": agent.config.clearance,
                    "status": agent_problem(loaded, agent) or "ok",
                }
            )
        return agents

    def create_task(
        self, title: str, body: str = "", agent: str | None = None, confidential: bool = False
    ) -> dict[str, Any]:
        loaded, conn = self._open()
        with closing(conn):
            classification = "confidential" if confidential else "public"
            task = repo.add_task(conn, title, body, agent=agent, classification=classification)
            repo.log_event(conn, "task.queued", agent=agent, task_id=task.id, data={"via": "mcp"})
            return self._task_view(loaded, task)

    def delegate(self, agent: str, task: str, title: str | None = None) -> dict[str, Any]:
        loaded, conn = self._open()
        with closing(conn):
            created = repo.add_task(conn, title or " ".join(task.split())[:80], task, agent=agent)
            repo.log_event(
                conn, "task.queued", agent=agent, task_id=created.id, data={"via": "mcp"}
            )
            outcome = runner.run_task(conn, loaded, created.id)
            return self._task_view(loaded, outcome.task)

    def task_status(self, task_id: int) -> dict[str, Any]:
        loaded, conn = self._open()
        with closing(conn):
            try:
                task = repo.get_task(conn, task_id)
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
            view = self._task_view(loaded, task)
            view["children"] = [self._task_view(loaded, c) for c in repo.child_tasks(conn, task_id)]
            return view

    def search_context(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Finished public task results and public agents' memory facts matching `query`."""
        loaded, conn = self._open()
        hits: list[dict[str, Any]] = []
        needle = f"%{query.strip()}%"
        with closing(conn):
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'done' AND classification = 'public'"
                " AND (title LIKE ? OR body LIKE ? OR result LIKE ?) ORDER BY id DESC LIMIT ?",
                (needle, needle, needle, limit),
            )
            for row in rows:
                task = repo._task(row)
                if not self._confidential_agent(loaded, task.agent):
                    hits.append(
                        {
                            "source": f"task #{task.id}",
                            "agent": task.agent,
                            "title": task.title,
                            "text": (task.result or "")[:MAX_RESULT_CHARS],
                        }
                    )
        lowered = query.strip().lower()
        for name in list_agent_names(loaded):
            if len(hits) >= limit or self._confidential_agent(loaded, name):
                continue
            for fact in memory.load(loaded.agents_dir / name):
                if lowered in fact.text.lower() or lowered in fact.id:
                    hits.append(
                        {
                            "source": f"memory {name}",
                            "agent": name,
                            "title": fact.id,
                            "text": fact.text,
                        }
                    )
        return hits[:limit]

    def write_memory(self, agent: str, fact_id: str, text: str) -> dict[str, Any]:
        loaded, conn = self._open()
        with closing(conn):
            try:
                target = load_agent(loaded, agent)
            except AgentError as exc:
                raise ValueError(str(exc)) from exc
            outcome = memory.apply(
                memory.load(target.dir),
                [{"op": "add", "id": fact_id, "text": text}],
                target.config.memory.max_facts,
            )
            if outcome.rejected:
                raise ValueError(f"rejected: {outcome.rejected[0][1]}")
            memory.save(target.dir, outcome.facts)
            op = outcome.applied[0]
            repo.log_event(
                conn, "memory.add", agent=agent, data={"id": op.id, "text": op.text, "by": "mcp"}
            )
            return {"agent": agent, "id": op.id, "text": op.text}


def build_server(tools: Tools | None = None) -> MCPServer:
    tools = tools or Tools()
    server = MCPServer(
        name="ao",
        instructions="agentOrchestrator: queue and delegate tasks to ao agents, check task "
        "status, search finished results and add agent memory. Budgets and guards apply.",
    )

    @server.tool(description="List ao agents with role, backend, clearance and status.")
    def list_agents() -> list[dict[str, Any]]:
        return tools.list_agents()

    @server.tool(description="Queue a task (not run). confidential=true keeps it off cloud models.")
    def create_task(
        title: str, body: str = "", agent: str | None = None, confidential: bool = False
    ) -> dict[str, Any]:
        return tools.create_task(title, body, agent, confidential)

    @server.tool(description="Create a task for an agent, run it now and return the result.")
    def delegate(agent: str, task: str, title: str | None = None) -> dict[str, Any]:
        return tools.delegate(agent, task, title)

    @server.tool(description="Status, result and children of a task.")
    def task_status(task_id: int) -> dict[str, Any]:
        return tools.task_status(task_id)

    @server.tool(description="Search finished task results and agent memory (public only).")
    def search_context(query: str, limit: int = 10) -> list[dict[str, Any]]:
        return tools.search_context(query, limit)

    @server.tool(description="Add or replace one memory fact (id: short slug) for an agent.")
    def write_memory(agent: str, fact_id: str, text: str) -> dict[str, Any]:
        return tools.write_memory(agent, fact_id, text)

    return server
