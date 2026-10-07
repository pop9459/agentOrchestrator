"""`ao` command-line entrypoint."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from typing import Annotated

import typer

from ao import __version__, budget, secrets
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
from ao.cli_common import (
    EXIT_REFUSED,
    EXIT_RUN_FAILED,
    EXIT_USAGE,
    load_or_exit,
    open_db,
    text_or_stdin,
)
from ao.cli_tasks import task_app
from ao.config import LoadedConfig, config_files, redact
from ao.db import repo, store
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
app.add_typer(task_app, name="task")


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
    force: Annotated[
        bool, typer.Option("--force", help="Run even if over budget (logged).")
    ] = False,
) -> None:
    """Run one agent on one prompt and record the run.

    Exit codes: 0 ok, 1 run failed or timed out, 2 bad input/config, 3 refused (budget).
    """
    prompt = text_or_stdin(prompt)
    loaded = load_or_exit()
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

    with closing(open_db(loaded)) as conn:
        record = runs.execute(conn, prepared, force=force)
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
    elif result.outcome == "refused":
        typer.echo(f"refused (run #{record.run_id}): {result.error}", err=True)
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
    if result.outcome == "refused":
        raise typer.Exit(EXIT_REFUSED)
    if result.outcome != "ok":
        raise typer.Exit(EXIT_RUN_FAILED)


@app.command("usage")
def usage(
    agent: Annotated[str | None, typer.Option("--agent", help="Only this agent.")] = None,
    since: Annotated[
        str, typer.Option("--since", help="today, Nd (last N days) or YYYY-MM-DD.")
    ] = "7d",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Token and cost totals per agent/backend/model, plus today's budget status."""
    try:
        start = budget.parse_since(since)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    loaded = load_or_exit()
    with closing(open_db(loaded)) as conn:
        rows = repo.usage_summary(conn, since=start.isoformat(), agent=agent)
        budgets = _budget_status(loaded, conn, agent)
    if as_json:
        typer.echo(json.dumps({
            "since": start.isoformat(),
            "rows": [{**asdict(r), "billable_tokens": r.billable_tokens} for r in rows],
            "budgets_today": budgets,
        }, indent=2))  # fmt: skip
        return
    typer.echo(f"usage since {start.isoformat()} (UTC)")
    if not rows:
        typer.echo("  no runs")
    else:
        header = ("agent", "backend", "model", "runs", "in", "out", "cache r", "cache w", "cost")
        table = [header] + [
            (r.agent, r.backend, r.model or "-", str(r.runs), f"{r.tokens_in:,}",
             f"{r.tokens_out:,}", f"{r.cache_read_tokens:,}", f"{r.cache_write_tokens:,}",
             f"${r.cost_usd:.4f}")
            for r in rows
        ]  # fmt: skip
        widths = [max(len(row[i]) for row in table) for i in range(len(header))]
        for row in table:
            typer.echo("  " + "  ".join(
                col.ljust(w) if i < 3 else col.rjust(w)
                for i, (col, w) in enumerate(zip(row, widths, strict=True))
            ))  # fmt: skip
        total = sum(r.billable_tokens for r in rows)
        typer.echo(f"  billable tokens (in + cache w + out): {total:,}"
                   f" · cost ${sum(r.cost_usd for r in rows):.4f}")  # fmt: skip
    if budgets:
        typer.echo("budgets today:")
        for line in budgets:
            typer.echo(f"  {line['scope']}: {line['used']} / {line['limit']}")


def _budget_status(
    loaded: LoadedConfig, conn: sqlite3.Connection, only_agent: str | None
) -> list[dict[str, str]]:
    """Human-readable used/limit lines for every configured daily cap."""
    day = budget.today()
    lines = []
    for name in list_agent_names(loaded):
        if only_agent and name != only_agent:
            continue
        try:
            cfg = load_agent(loaded, name).config.budget
        except AgentError:
            continue
        spend = budget.agent_spend(conn, name, day)
        if cfg.daily_tokens is not None:
            lines.append({"scope": f"{name} tokens", "used": f"{spend.tokens:,}",
                          "limit": f"{cfg.daily_tokens:,}"})  # fmt: skip
        if cfg.daily_cost_usd is not None:
            lines.append({"scope": f"{name} cost", "used": f"${spend.cost_usd:.4f}",
                          "limit": f"${cfg.daily_cost_usd:.4f}"})  # fmt: skip
    cap = loaded.config.budgets.global_daily_tokens
    if cap is not None and not only_agent:
        local = {n for n, b in loaded.config.backends.items() if b.is_local}
        used = budget.cloud_tokens(conn, local, day)
        lines.append({"scope": "global cloud tokens", "used": f"{used:,}", "limit": f"{cap:,}"})
    return lines


@config_app.command("show")
def config_show(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Print the merged config (secrets redacted) and which files it came from."""
    loaded = load_or_exit()
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
    loaded = load_or_exit()
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
    db_path = load_or_exit().data.ensure().db_path
    with closing(store.connect(db_path)) as conn:
        applied = store.migrate(conn)
    if not applied:
        typer.echo(f"{db_path}: up to date")
    for migration in applied:
        typer.echo(f"{db_path}: applied {migration.version:04d}_{migration.name}")


@db_app.command("status")
def db_status() -> None:
    """Show database location, applied migrations and row counts."""
    db_path = load_or_exit().data.db_path
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
    loaded = load_or_exit()
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
    loaded = load_or_exit()
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
    loaded = load_or_exit()
    try:
        directory = create_agent(loaded, name, template)
    except AgentError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"created {directory}; edit agent.toml and INSTRUCTIONS.md")


@agents_app.command("init")
def agents_init() -> None:
    """Create the starter agents (every template except `blank`) that don't exist yet."""
    loaded = load_or_exit()
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
