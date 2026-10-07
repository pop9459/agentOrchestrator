-- Refused runs never reached a backend; keep them in `runs`/`events` but out of usage.

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
WHERE outcome != 'refused'
GROUP BY day, agent, backend, model;
