# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is
`ao` (agentOrchestrator): a simplified, token-efficient alternative to Paperclip. It runs a small "company" of hireable agents. The main backend is the Claude Code CLI (`claude -p`, Claude Pro subscription). Other backends plug in: a llama.cpp server on a separate LAN PC (not this laptop), any OpenAI-compatible API, or other agent CLIs. Linear is the human-facing task overview; there is a CLI now and a TUI later.

Roadmap and tickets: Linear project **P-KAP-10** "Create paperclip clone project" (team KAPSLOK, issues KAP-73…98 plus KAP-45/46/47), https://linear.app/pop9459/project/create-paperclip-clone-project-8e1a10e04d56. Milestones run M0 Foundations → M1 Agent runtime → M2 Company core → M3 Confidential & local → M4 Linear → M5 Connectors → M6 TUI. Branch names follow Linear's `pop9459/kap-NN-…` convention.

**Status:** M0 done. M1 done for the Claude Code backend (agents, `ao run`, per-agent MCP, budgets, `ao usage`). The local/OpenAI-compatible (KAP-80) and CLI-template (KAP-81) backends are deferred. Next up is M2 (task runner KAP-83, context builder KAP-84).

## Commands
Python ≥3.12 managed by uv; package lives in `src/ao/`, tests in `tests/`.
```bash
uv sync                                   # install deps + dev group into .venv
uv run ao --help                          # run the CLI
uv run pytest                             # all tests
uv run pytest tests/test_cli.py::test_version   # single test
uv run ruff check                         # lint (add --fix to autofix)
uv run ruff format                        # format
AO_LIVE=1 uv run pytest -m live           # opt-in tests that call real `claude -p` (tiny quota use)
uv run ao run <agent> "…" --dry-run       # show exact claude argv + system prompt, call nothing
```

## Design rules (these drive most implementation decisions)
1. **No heartbeats.** The LLM is called only when a task or event exists. Queues, scheduling, routing, retries and budgets are plain Python.
2. **Minimal context per run.** Prompts are assembled by the context builder in the order INSTRUCTIONS → memory → attached docs → task (stable → variable, for prompt caching). Never inject company-wide state.
3. **Structured output for control flow.** Delegation (Jarvis), hiring and memory updates are JSON or MCP tool calls, parsed by code. Never parse them from free text.
4. **Budgets are hard limits.** Every run records tokens. Exceeding a per-agent or global cap refuses the run. Pro limits are shared with interactive Claude Code use.
5. **Confidentiality is enforced in code.** Confidential data never reaches a non-local backend. If the local backend is down, the task waits in the queue and never falls back to the cloud. Only an approved summary crosses the airlock.
6. **Secrets live only in connector/config code** (keyring or env). They never go into agent dirs, prompts, logs or the DB.
7. **Runtime output is user data, never repo content.** The repo is public. Everything ao creates or changes at runtime stays gitignored or under the data dir: `agents/`, memory, workspaces, the DB, `.env`, `ao.local.toml`. Only generic templates and examples are committed.
8. **Outward actions need human approval.** The orchestrator must not write to Linear until the KAP-95 approval gate exists, and then only to allowlisted projects. Mail is read-only permanently.

## Config, secrets and paths (implemented)
- `ao.config.load_config()` deep-merges `~/.config/ao/ao.toml` < `<project>/ao.toml` < `<project>/ao.local.toml` (gitignored) into strict Pydantic models (`extra="forbid"`; errors give dotted key paths and the source file). See `ao.example.toml`. The project root is the nearest ancestor containing `ao.toml`/`ao.local.toml`/`.git`.
- Config holds only secret *names* (`api_key_secret = "llama"`). `ao.secrets.get_secret(name)` resolves `AO_SECRET_<NAME>` env, then project `.env`, then the OS keyring (service `ao`, optional `keyring` extra). `.env` values are deliberately **not** exported to `os.environ`. When spawning agent subprocesses, also strip `AO_SECRET_*` from the child env.
- Data dir: `AO_DATA_DIR` > `paths.data_dir` > `$XDG_DATA_HOME/ao` (`db/ao.sqlite3`, `cache/`, `logs/`). Tests isolate all of this via the autouse fixture in `tests/conftest.py`.

## State store (implemented)
- `ao.db.store`: `connect()` opens SQLite in autocommit mode with foreign keys and WAL on. `migrate()` applies `src/ao/db/migrations/NNNN_name.sql` in order, each inside its own transaction, and records them in `schema_migrations`. A schema change means a **new** numbered file; never edit an applied migration.
- `ao.db.repo`: thin functions returning dataclasses (`add_task`, `record_run`, `log_event`, `usage_summary`, …). Use `repo.transaction(conn)` for multi-statement writes.
- Usage is the `usage_daily` **view** over `runs`, not separate counters, so recording a run is the only bookkeeping needed.
- `ao db migrate` / `ao db status`.

## Agents (implemented)
- `ao.agents`: `agents/<name>/` (gitignored) holds `agent.toml` (`AgentConfig`: role, backend, model, clearance, `[prompt] mode`, `[tools]`, `[limits]`, `[budget]`), `INSTRUCTIONS.md`, optional `mcp.json` and optional `memory/INDEX.md`. Templates live in `src/ao/agent_templates/`; `ao agents init|new|list|show|templates`.
- The run **workspace (cwd) is `<data>/workspaces/<name>`, deliberately outside the repo**. With a cwd inside the repo, Claude Code auto-loads this CLAUDE.md into every agent run (+1.8k tokens; measured in KAP-102).
- Prefer `prompt.mode = "replace"` with `tools.builtin = []` (about 430 input tokens per call). `append` plus tools costs about 17.6k. Only give agents tools they need.
- A built-in `claude` backend (`claude_code`) exists without any config file.
- MCP (`ao.mcp`): `--strict-mcp-config` is **always** on, so an agent gets only the servers in its own `mcp.json`. Without strict mode your global claude.ai connectors added ~30k input tokens per run (measured). `mcp.json` is validated at load. Secrets are written as `{{secret:name}}` and rendered into a 0600 temp copy that exists only during the run.

## Backends and runs (implemented)
- `ao.backends.base`: `RunRequest` → `Backend.run()` → `Result` (`Usage` counts input, output, cache read/write and cost; `billable_tokens` excludes cache reads). Backends register with `@register("<type>")` in `ao.backends`; built-ins are imported at the bottom of `ao/backends/__init__.py`.
- `ao.backends.claude_code`: `build_argv()` is a pure function, and its fixed flags (`FIXED_FLAGS`) come from the KAP-102 measurements. The prompt is sent on **stdin**. The child env drops `AO_SECRET_*` and, with `use_subscription` (default), `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`. Timeouts kill the whole process group. `parse_result()` is tested against a real, scrubbed output in `tests/fixtures/`.
- `ao.run`: `prepare()` (agent → backend → `RunRequest`; system prompt = INSTRUCTIONS then memory index) and `execute()` (runs the backend, writes a `runs` row and a `run.finished` event). CLI: `ao run`. Tests swap the backend for `tests/fakes.py:FakeBackend` by monkeypatching `ao.run.get_backend`.

## Tasks and runner (implemented)
- `ao.runner.run_task()` is the only way tasks execute: status `queued|waiting` → `running` → `done` (result stored) / `waiting` (budget refused) / `failed`. It is sequential and on demand (`ao task run N|--next|--all`). There is no daemon and no polling.
- In-code guards: `MAX_ATTEMPTS` loop guard (`ao task retry` resets it), and confidential tasks are hard-refused on non-local backends before anything is sent (a minimal version of KAP-90).
- The CLI is split into `cli.py` (app, run/usage/config/db/agents), `cli_tasks.py` and `cli_common.py` (shared `load_or_exit`, `open_db`, exit codes).

## Budgets (implemented)
- `ao.budget.check()` runs inside `run.execute()` **before** the backend. Agent `daily_tokens` (billable = input + cache writes + output), agent `daily_cost_usd` (CLI notional cost) and global `budgets.global_daily_tokens` (non-local backends only) are counted per UTC day. Over a cap → the run is recorded as `refused` and the backend is never called. `--force` runs anyway and logs a `budget.override` event. `per_run_cost_usd` maps to `--max-budget-usd`.
- `usage_daily` excludes refused runs. `ao usage [--agent] [--since today|Nd|YYYY-MM-DD] [--json]`. `ao run` exit codes: 0 ok, 1 failed/timeout, 2 bad input, 3 refused.

## Planned architecture
- **More backends:** `openai_compat` (llama.cpp, KAP-80) and `cli_template` (KAP-81) are deferred. Verify Claude CLI flags against `claude --help`; don't trust memory.
- **Runner**: pick task → health check → routing/confidentiality guard → budget check → context builder → backend → persist run → apply memory proposals.
- **State**: SQLite (tasks, runs, usage, events/audit). Linear is the source of truth for human-visible work.
- **Jarvis** sees only the agent roster and returns a delegation plan. The runner executes it. `ao` is also exposed as an MCP server so Claude-based agents delegate through tool calls.
- **Connectors** (calendar ICS, IMAP mail, Blackboard via Playwright + TOTP) are pull-based and produce classified Documents in a local cache. Agents fetch them on demand.
