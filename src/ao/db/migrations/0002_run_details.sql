-- Cache token split, session and turn count per run; usage view gains cache columns.

ALTER TABLE runs ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0);
ALTER TABLE runs ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0);
ALTER TABLE runs ADD COLUMN session_id TEXT;
ALTER TABLE runs ADD COLUMN num_turns INTEGER;

DROP VIEW usage_daily;
CREATE VIEW usage_daily AS
SELECT substr(started_at, 1, 10)  AS day,
       agent,
       backend,
       model,
       count(*)                   AS runs,
       sum(tokens_in)             AS tokens_in,
       sum(tokens_out)            AS tokens_out,
       sum(cache_read_tokens)     AS cache_read_tokens,
       sum(cache_write_tokens)    AS cache_write_tokens,
       sum(coalesce(cost_usd, 0)) AS cost_usd
FROM runs
GROUP BY day, agent, backend, model;
