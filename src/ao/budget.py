"""Daily budgets, checked before every run. Exceeding a cap refuses the run outright.

Counted per UTC day from the `runs` table (via `usage_daily`):
- agent `daily_tokens`: billable tokens (input + cache writes + output; cache reads excluded)
- agent `daily_cost_usd`: the CLI's notional cost (weights model and caching; a decent
  proxy for how fast the Pro plan's limits are consumed)
- `budgets.global_daily_tokens`: billable tokens across all non-local backends
The per-run cap (`per_run_cost_usd`) is enforced by the backend itself (`--max-budget-usd`).
"""

import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ao.agents import BudgetConfig
from ao.config import BudgetsConfig
from ao.db import repo


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class Spend:
    tokens: int
    cost_usd: float


def today() -> date:
    return datetime.now(UTC).date()


def parse_since(value: str, now: date | None = None) -> date:
    """`today`, `Nd` (last N days including today) or `YYYY-MM-DD`."""
    now = now or today()
    if value == "today":
        return now
    if match := re.fullmatch(r"(\d+)d", value):
        days = int(match[1])
        if days < 1:
            raise ValueError("Nd needs N >= 1")
        return now - timedelta(days=days - 1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid --since {value!r}: use today, 7d or YYYY-MM-DD") from exc


def agent_spend(conn: sqlite3.Connection, agent: str, day: date) -> Spend:
    rows = repo.usage_summary(conn, since=day.isoformat(), agent=agent)
    return Spend(sum(r.billable_tokens for r in rows), sum(r.cost_usd for r in rows))


def cloud_tokens(conn: sqlite3.Connection, local_backends: Collection[str], day: date) -> int:
    rows = repo.usage_summary(conn, since=day.isoformat())
    return sum(r.billable_tokens for r in rows if r.backend not in local_backends)


def check(
    conn: sqlite3.Connection,
    agent: str,
    budget: BudgetConfig,
    *,
    backend_is_local: bool,
    global_budgets: BudgetsConfig,
    local_backends: Collection[str],
    day: date | None = None,
) -> Decision:
    day = day or today()
    spend = agent_spend(conn, agent, day)
    if budget.daily_tokens is not None and spend.tokens >= budget.daily_tokens:
        return Decision(
            False, f"agent {agent!r} used {spend.tokens:,} of {budget.daily_tokens:,} daily tokens"
        )
    if budget.daily_cost_usd is not None and spend.cost_usd >= budget.daily_cost_usd:
        used_usd, cap_usd = spend.cost_usd, budget.daily_cost_usd
        return Decision(False, f"agent {agent!r} used ${used_usd:.4f} of ${cap_usd:.4f} daily cost")
    cap = global_budgets.global_daily_tokens
    if cap is not None and not backend_is_local:
        used = cloud_tokens(conn, local_backends, day)
        if used >= cap:
            return Decision(False, f"global cloud budget used: {used:,} of {cap:,} daily tokens")
    return Decision(True)
