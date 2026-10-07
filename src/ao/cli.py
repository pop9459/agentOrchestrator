"""`ao` command-line entrypoint."""

import json
import sqlite3
import sys
from contextlib import closing
from typing import Annotated

import typer

from ao import __version__, secrets
from ao import run as runs
from ao.agents import (
    AgentError,
    agent_problem,
    create_agent,
    list_agent_names,
    load_agent,
    template_names,
)
from ao.backends.base import BackendError
from ao.config import ConfigError, LoadedConfig, config_files, load_config, redact
from ao.db import store
from ao.paths import find_project_root

app = typer.Typer(
    name="ao",
    help="agentOrchestrator: a small, token-efficient company of hireable agents.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
db_app = typer.Typer(help="Manage the state database.", no_args_is_help=True)
app.add_typer(db_app, name="db")
agents_app = typer.Typer(help="Create and inspect agents.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ao {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """agentOrchestrator CLI."""
    secrets.load_dotenv_file(find_project_root())


def _load() -> LoadedConfig:
    try:
        return load_config()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _open_db(loaded: LoadedConfig) -> sqlite3.Connection:
    """Open the state DB, creating/upgrading it as needed (migrations are cheap no-ops)."""
    conn = store.connect(loaded.data.ensure().db_path)
    store.migrate(conn)
    return conn


EXIT_RUN_FAILED = 1
EXIT_USAGE = 2


@app.command("run")
def run_agent(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    prompt: Annotated[
        str | None, typer.Argument(help="Prompt; read from stdin if omitted.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be sent; call nothing.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Run one agent on one prompt and record the run."""
    if prompt is None:
        if sys.stdin.isatty():
            typer.echo("no prompt given (pass it as an argument or on stdin)", err=True)
            raise typer.Exit(EXIT_USAGE)
        prompt = sys.stdin.read()
    loaded = _load()
    try:
        prepared = runs.prepare(loaded, agent, prompt)
    except (AgentError, BackendError, runs.RunError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_USAGE) from exc

    if dry_run:
        typer.echo(f"# backend: {prepared.backend.name} ({prepared.backend.config.type})")
        typer.echo(f"# cwd: {prepared.agent.workspace}")
        typer.echo(
            f"# estimated tokens (ours, excl. backend overhead): {prepared.estimated_tokens}"
        )
        typer.echo(prepared.backend.preview(prepared.request))
        typer.echo("# --- system prompt ---")
        typer.echo(prepared.request.system_prompt)
        typer.echo("# --- prompt ---")
        typer.echo(prepared.request.prompt)
        return

    with closing(_open_db(loaded)) as conn:
        record = runs.execute(conn, prepared)
    result, usage = record.result, record.result.usage
    if as_json:
        typer.echo(json.dumps({
            "run_id": record.run_id, "outcome": result.outcome, "text": result.text,
            "model": result.model, "error": result.error, "duration_ms": result.duration_ms,
            "usage": {
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens, "cost_usd": usage.cost_usd,
            },
        }, indent=2))  # fmt: skip
    else:
        if result.text:
            typer.echo(result.text)
        cost = f" · ${usage.cost_usd:.4f}" if usage.cost_usd is not None else ""
        secs = f" · {result.duration_ms / 1000:.1f}s" if result.duration_ms is not None else ""
        typer.echo(
            f"[{result.outcome} · {result.model or '?'} · in {usage.input_tokens}"
            f" out {usage.output_tokens} cache r{usage.cache_read_tokens}"
            f" w{usage.cache_write_tokens}{cost}{secs} · run #{record.run_id}]",
            err=True,
        )
        if result.error:
            typer.echo(f"error: {result.error}", err=True)
    if result.outcome != "ok":
        raise typer.Exit(EXIT_RUN_FAILED)


@config_app.command("show")
def config_show(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Print the merged config (secrets redacted) and which files it came from."""
    loaded = _load()
    config = redact(loaded.config.model_dump(mode="json"))
    secret_refs = {
        name: backend.api_key_secret
        for name, backend in loaded.config.backends.items()
        if backend.api_key_secret
    }
    secret_status = {ref: secrets.secret_source(ref) or "missing" for ref in secret_refs.values()}
    if as_json:
        payload = {
            "project_root": str(loaded.project_root),
            "sources": [str(p) for p in loaded.sources],
            "config": config,
            "secrets": secret_status,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"project root: {loaded.project_root}")
    typer.echo("sources:" if loaded.sources else "sources: (none, using defaults)")
    for path in loaded.sources:
        typer.echo(f"  {path}")
    typer.echo(json.dumps(config, indent=2))
    if secret_status:
        typer.echo("secrets:")
        for ref, source in secret_status.items():
            typer.echo(f"  {ref}: {source}")


@config_app.command("paths")
def config_paths() -> None:
    """Show where ao looks for config and stores data."""
    loaded = _load()
    data = loaded.data
    typer.echo(f"project root: {loaded.project_root}")
    typer.echo("config files (low → high precedence):")
    for path in config_files(loaded.project_root):
        typer.echo(f"  [{'x' if path.is_file() else ' '}] {path}")
    typer.echo(f"agents dir:   {loaded.agents_dir}")
    typer.echo(f"data dir:     {data.root}")
    typer.echo(f"  database:   {data.db_path}")
    typer.echo(f"  cache:      {data.cache}")
    typer.echo(f"  logs:       {data.logs}")


@db_app.command("migrate")
def db_migrate() -> None:
    """Create or upgrade the state database (safe to run repeatedly)."""
    db_path = _load().data.ensure().db_path
    with closing(store.connect(db_path)) as conn:
        applied = store.migrate(conn)
    if not applied:
        typer.echo(f"{db_path}: up to date")
    for migration in applied:
        typer.echo(f"{db_path}: applied {migration.version:04d}_{migration.name}")


@db_app.command("status")
def db_status() -> None:
    """Show database location, applied migrations and row counts."""
    db_path = _load().data.db_path
    typer.echo(f"database: {db_path}")
    if not db_path.exists():
        typer.echo("not created yet; run `ao db migrate`")
        raise typer.Exit(1)
    with closing(store.connect(db_path)) as conn:
        applied = store.applied_versions(conn)
        pending = [m for m in store.available_migrations() if m.version not in applied]
        typer.echo(f"applied:  {', '.join(f'{v:04d}' for v in applied) or '(none)'}")
        if pending:
            typer.echo(f"pending:  {', '.join(f'{m.version:04d}' for m in pending)}")
        if applied:
            for table in ("tasks", "runs", "events"):
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                typer.echo(f"{table + ':':<9} {count}")


@agents_app.command("list")
def agents_list() -> None:
    """List agents with their backend, model and status."""
    loaded = _load()
    names = list_agent_names(loaded)
    if not names:
        typer.echo(f"no agents in {loaded.agents_dir}; create some with `ao agents init`")
        return
    rows = []
    for name in names:
        try:
            agent = load_agent(loaded, name)
        except AgentError as exc:
            rows.append((name, "-", "-", "-", f"invalid: {str(exc).splitlines()[-1].strip()}"))
            continue
        cfg = agent.config
        backend = loaded.config.backends.get(cfg.backend)
        model = cfg.model or (backend.model if backend else None)
        rows.append(
            (name, f"{cfg.backend}/{model or 'default'}", cfg.clearance, cfg.role,
             agent_problem(loaded, agent) or "ok")
        )  # fmt: skip
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for row in rows:
        head = "  ".join(col.ljust(w) for col, w in zip(row[:3], widths, strict=True))
        typer.echo(f"{head}  [{row[4]}]  {row[3]}")


@agents_app.command("show")
def agents_show(name: str) -> None:
    """Show an agent's resolved config, files and workspace."""
    loaded = _load()
    try:
        agent = load_agent(loaded, name)
    except AgentError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"agent:        {agent.name}")
    typer.echo(f"dir:          {agent.dir}")
    typer.echo(f"workspace:    {agent.workspace}")
    typer.echo(f"mcp.json:     {agent.mcp_config or '(none: no MCP servers)'}")
    typer.echo(f"memory index: {'yes' if agent.memory_index else 'no'}")
    typer.echo(f"status:       {agent_problem(loaded, agent) or 'ok'}")
    typer.echo(json.dumps(agent.config.model_dump(mode="json"), indent=2))


@agents_app.command("new")
def agents_new(
    name: str,
    template: Annotated[
        str | None, typer.Option("--from", help="Template to copy (see `ao agents templates`).")
    ] = None,
) -> None:
    """Create a new agent from a template."""
    loaded = _load()
    try:
        directory = create_agent(loaded, name, template)
    except AgentError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"created {directory}; edit agent.toml and INSTRUCTIONS.md")


@agents_app.command("init")
def agents_init() -> None:
    """Create the starter agents (every template except `blank`) that don't exist yet."""
    loaded = _load()
    existing = set(list_agent_names(loaded))
    for name in template_names():
        if name == "blank" or name in existing:
            continue
        typer.echo(f"created {create_agent(loaded, name)}")


@agents_app.command("templates")
def agents_templates() -> None:
    """List available agent templates."""
    for name in template_names():
        typer.echo(name)
