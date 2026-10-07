"""`ao memory …` commands."""

from contextlib import closing
from typing import Annotated

import typer

from ao import compaction, memory
from ao.agents import AgentError, load_agent
from ao.cli_common import fail, load_or_exit, open_db
from ao.db import repo

memory_app = typer.Typer(help="Inspect and edit agent memory.", no_args_is_help=True)


def _agent(name: str):
    loaded = load_or_exit()
    try:
        return loaded, load_agent(loaded, name)
    except AgentError as exc:
        raise fail(str(exc)) from exc


@memory_app.command("show")
def memory_show(agent: str) -> None:
    """Print an agent's memory index."""
    _, loaded_agent = _agent(agent)
    facts = memory.load(loaded_agent.dir)
    cfg = loaded_agent.config.memory
    typer.echo(f"# {agent}: {len(facts)}/{cfg.max_facts} facts · writeback "
               f"{'on' if cfg.writeback else 'off'}")  # fmt: skip
    for fact in facts:
        typer.echo(fact.line())


def _edit(agent: str, ops: list[dict]) -> None:
    loaded, loaded_agent = _agent(agent)
    outcome = memory.apply(memory.load(loaded_agent.dir), ops, loaded_agent.config.memory.max_facts)
    if outcome.rejected:
        raise fail(f"rejected: {outcome.rejected[0][1]}")
    memory.save(loaded_agent.dir, outcome.facts)
    with closing(open_db(loaded)) as conn:
        for op in outcome.applied:
            repo.log_event(conn, f"memory.{op.op}", agent=agent,
                           data={"id": op.id, "text": op.text, "by": "user"})  # fmt: skip
    typer.echo("ok")


@memory_app.command("add")
def memory_add(agent: str, fact_id: str, text: str) -> None:
    """Add or replace a fact: `ao memory add jarvis tone "User likes brief answers"`."""
    _edit(agent, [{"op": "add", "id": fact_id, "text": text}])


@memory_app.command("rm")
def memory_rm(agent: str, fact_id: str) -> None:
    """Delete a fact by id."""
    _edit(agent, [{"op": "delete", "id": fact_id}])


@memory_app.command("compact")
def memory_compact(
    agent: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without asking.")] = False,
    model: Annotated[str | None, typer.Option("--model", help="Override the model.")] = None,
) -> None:
    """Merge/prune facts with one cheap model call; shows a diff before applying."""
    loaded, loaded_agent = _agent(agent)
    with closing(open_db(loaded)) as conn:
        try:
            proposal = compaction.propose(conn, loaded, agent, model)
        except compaction.CompactionError as exc:
            raise fail(str(exc)) from exc
        diff = proposal.diff()
        typer.echo(f"{len(proposal.before)} → {len(proposal.after)} facts (run #{proposal.run_id})")
        if not diff:
            typer.echo("no changes")
            return
        typer.echo("\n".join(diff))
        if not yes and not typer.confirm("apply?", default=False):
            typer.echo("left unchanged")
            return
        memory.save(loaded_agent.dir, proposal.after)
        sizes = {"before": len(proposal.before), "after": len(proposal.after)}
        repo.log_event(conn, "memory.compact", agent=agent, run_id=proposal.run_id, data=sizes)
    typer.echo("applied")
