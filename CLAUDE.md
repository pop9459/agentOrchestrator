# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is
`ao` (agentOrchestrator): a simplified, token-efficient alternative to Paperclip. It runs a small "company" of hireable agents. The main backend is the Claude Code CLI (`claude -p`, Claude Pro subscription). Other backends plug in: a llama.cpp server on a separate LAN PC (not this laptop), any OpenAI-compatible API, or other agent CLIs. Linear is the human-facing task overview; there is a CLI now and a TUI later.

Roadmap and tickets: Linear project **P-KAP-10** "Create paperclip clone project" (team KAPSLOK, issues KAP-73…98 plus KAP-45/46/47), https://linear.app/pop9459/project/create-paperclip-clone-project-8e1a10e04d56. Milestones run M0 Foundations → M1 Agent runtime → M2 Company core → M3 Confidential & local → M4 Linear → M5 Connectors → M6 TUI. Branch names follow Linear's `pop9459/kap-NN-…` convention.

**Status:** M0 Foundations in progress.

## Commands
Python ≥3.12 managed by uv; package lives in `src/ao/`, tests in `tests/`.
```bash
uv sync                                   # install deps + dev group into .venv
uv run ao --help                          # run the CLI
uv run pytest                             # all tests
uv run pytest tests/test_cli.py::test_version   # single test
uv run ruff check                         # lint (add --fix to autofix)
uv run ruff format                        # format
```

## Design rules (these drive most implementation decisions)
1. **No heartbeats.** The LLM is called only when a task or event exists. Queues, scheduling, routing, retries and budgets are plain Python.
2. **Minimal context per run.** Prompts are assembled by the context builder in the order INSTRUCTIONS → memory → attached docs → task (stable → variable, for prompt caching). Never inject company-wide state.
3. **Structured output for control flow.** Delegation (Jarvis), hiring and memory updates are JSON or MCP tool calls, parsed by code. Never parse them from free text.
4. **Budgets are hard limits.** Every run records tokens. Exceeding a per-agent or global cap refuses the run. Pro limits are shared with interactive Claude Code use.
5. **Confidentiality is enforced in code.** Confidential data never reaches a non-local backend. If the local backend is down, the task waits in the queue and never falls back to the cloud. Only an approved summary crosses the airlock.
6. **Secrets live only in connector/config code** (keyring or env). They never go into agent dirs, prompts, logs or the DB.
7. **Outward actions need human approval.** The orchestrator must not write to Linear until the KAP-95 approval gate exists, and then only to allowlisted projects. Mail is read-only permanently.

## Config, secrets and paths (implemented)
- `ao.config.load_config()` deep-merges `~/.config/ao/ao.toml` < `<project>/ao.toml` < `<project>/ao.local.toml` (gitignored) into strict Pydantic models (`extra="forbid"`; errors give dotted key paths and the source file). See `ao.example.toml`. The project root is the nearest ancestor containing `ao.toml`/`ao.local.toml`/`.git`.
- Config holds only secret *names* (`api_key_secret = "llama"`). `ao.secrets.get_secret(name)` resolves `AO_SECRET_<NAME>` env, then project `.env`, then the OS keyring (service `ao`, optional `keyring` extra). `.env` values are deliberately **not** exported to `os.environ`. When spawning agent subprocesses, also strip `AO_SECRET_*` from the child env.
- Data dir: `AO_DATA_DIR` > `paths.data_dir` > `$XDG_DATA_HOME/ao` (`db/ao.sqlite3`, `cache/`, `logs/`). Tests isolate all of this via the autouse fixture in `tests/conftest.py`.

## Planned architecture
- **Agents are directories** `agents/<name>/`: `agent.toml` (backend, model, permissions, clearance, budget), `INSTRUCTIONS.md`, `memory/` (INDEX.md plus fact files), `workspace/` (cwd for runs), optional `mcp.json` (loaded with strict MCP config, so the agent inherits no global servers).
- **Backends** implement one interface, `run(prompt, context, opts) -> Result(text, usage, …)` plus `health()`. The implementations are `claude_code`, `openai_compat` and `cli_template`. Verify Claude CLI flags against `claude --help`; don't trust memory.
- **Runner**: pick task → health check → routing/confidentiality guard → budget check → context builder → backend → persist run → apply memory proposals.
- **State**: SQLite (tasks, runs, usage, events/audit). Linear is the source of truth for human-visible work.
- **Jarvis** sees only the agent roster and returns a delegation plan. The runner executes it. `ao` is also exposed as an MCP server so Claude-based agents delegate through tool calls.
- **Connectors** (calendar ICS, IMAP mail, Blackboard via Playwright + TOTP) are pull-based and produce classified Documents in a local cache. Agents fetch them on demand.
