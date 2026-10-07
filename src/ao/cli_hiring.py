"""`ao hire`."""

from contextlib import closing
from typing import Annotated

import typer

from ao import hiring
from ao.agents import AgentError, render_config, write_agent
from ao.cli_common import fail, load_or_exit, open_db, text_or_stdin
from ao.db import repo


def hire(
    need: Annotated[str | None, typer.Argument(help="What the new agent should do.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Create without asking.")] = False,
) -> None:
    """Ask the hiring agent to design a new agent; preview, then create on approval."""
    text = text_or_stdin(need, "need")
    loaded = load_or_exit()
    if hiring.ensure_hiring_agent(loaded):
        typer.echo("(created the hiring agent from its template)", err=True)
    with closing(open_db(loaded)) as conn:
        try:
            proposal = hiring.propose(conn, loaded, text)
        except (hiring.HiringError, AgentError) as exc:
            raise fail(str(exc)) from exc
        typer.echo(f"# proposed agent: {proposal.name}  (run #{proposal.run_id})")
        typer.echo(f"# reasoning: {proposal.reasoning}")
        for warning in proposal.warnings:
            typer.echo(f"# warning: {warning}")
        typer.echo("# --- agent.toml ---")
        typer.echo(render_config(proposal.config).rstrip())
        typer.echo("# --- INSTRUCTIONS.md ---")
        typer.echo(proposal.instructions)
        if not yes and not typer.confirm(f"hire {proposal.name}?", default=False):
            typer.echo("not hired; nothing written")
            return
        try:
            directory = write_agent(loaded, proposal.name, proposal.config, proposal.instructions)
        except AgentError as exc:
            raise fail(str(exc)) from exc
        repo.log_event(
            conn,
            "agent.hired",
            agent=proposal.name,
            run_id=proposal.run_id,
            data={"backend": proposal.config.backend, "model": proposal.config.model},
        )
    typer.echo(f"hired {proposal.name}: {directory}")
