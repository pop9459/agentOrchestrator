-- Core state: tasks, runs (one per backend call), events (audit log), daily usage view.
-- Timestamps are ISO-8601 UTC text.

CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    agent           TEXT,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed', 'canceled')),
    parent_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    classification  TEXT NOT NULL DEFAULT 'public'
                    CHECK (classification IN ('public', 'confidential')),
    linear_issue_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);

CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    agent        TEXT NOT NULL,
    backend      TEXT NOT NULL,
    model        TEXT,
    prompt_hash  TEXT,
    tokens_in    INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out   INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd     REAL,
    duration_ms  INTEGER,
    outcome      TEXT NOT NULL CHECK (outcome IN ('ok', 'error', 'refused', 'timeout')),
    error        TEXT,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at  TEXT
);
CREATE INDEX idx_runs_agent_started ON runs(agent, started_at);
CREATE INDEX idx_runs_task ON runs(task_id);

CREATE TABLE events (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind     TEXT NOT NULL,
    agent    TEXT,
    task_id  INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    run_id   INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    data     TEXT CHECK (data IS NULL OR json_valid(data))
);
CREATE INDEX idx_events_ts ON events(ts);

-- Usage is derived from runs rather than kept as separate counters.
CREATE VIEW usage_daily AS
SELECT substr(started_at, 1, 10) AS day,
       agent,
       backend,
       model,
       count(*)                   AS runs,
       sum(tokens_in)             AS tokens_in,
       sum(tokens_out)            AS tokens_out,
       sum(coalesce(cost_usd, 0)) AS cost_usd
FROM runs
GROUP BY day, agent, backend, model;
