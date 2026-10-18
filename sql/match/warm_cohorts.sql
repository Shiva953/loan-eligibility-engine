-- Warm match cache (called by Workflow A after every crawl): which cohorts and criteria the
-- active products can produce have no cached verdict or compiled rule yet?
--
-- Same output shape as uncached_cohorts.sql, so the same Code nodes build the requests; the
-- difference is the source: cohort_space() enumerates cohorts from the products instead of
-- from a batch's users. After this ran, a batch finds everything cached and Workflow B makes
-- no LLM call. Cheap when there is nothing to do: one query, no request. It reads app_config
-- itself (returned as config for the Code nodes), so the warm step is one round trip.
--
-- Parameters: #1 banding mode, #2 match_llm_enabled, #3 match_rules_enabled
--             ('' = from app_config, as in the workflow; the bench passes values), #4 execution id
--
-- JIT is off for this statement: the planner's estimate for the cohort space is huge (it can't
-- see how few criteria are fuzzy), so JIT compiled a 2-5 ms query for 0.5 s locally and 5 s in
-- a fresh n8n connection. n8n sends both statements as one message, so SET LOCAL ends with it.
SET LOCAL jit = off;

WITH config AS (
    SELECT jsonb_object_agg(key, value) AS config FROM app_config
),
settings AS (
    SELECT c.config,
           coalesce(nullif($1, ''), c.config ->> 'cohort_bands') AS cohort_bands,
           coalesce(nullif($2, ''), c.config ->> 'match_llm_enabled')::boolean AS llm,
           coalesce(nullif($3, ''), c.config ->> 'match_rules_enabled')::boolean AS rules
    FROM config c
),
-- Products that share a criterion share its cohorts; one wording per key is enough. One branch
-- per banding mode, so the mode is a literal and cohort_space() is planned for it (as a column
-- it inflated the cost estimate to 10^9 and JIT compilation took 0.5-5 s for a 2 ms query);
-- the one-time filter runs only the configured branch.
space AS MATERIALIZED (
    SELECT DISTINCT ON (c.criterion_key, c.signature)
           c.criterion_key, c.criterion_text, c.depends_on, c.signature
    FROM (
        SELECT * FROM cohort_space('threshold')
        WHERE (SELECT cohort_bands NOT IN ('coarse', 'fine', 'exact') FROM settings)
        UNION ALL
        SELECT * FROM cohort_space('coarse') WHERE (SELECT cohort_bands = 'coarse' FROM settings)
        UNION ALL
        SELECT * FROM cohort_space('fine') WHERE (SELECT cohort_bands = 'fine' FROM settings)
    ) c
    WHERE (SELECT llm FROM settings)
    ORDER BY c.criterion_key, c.signature, c.criterion_text
),
uncached AS MATERIALIZED (
    SELECT s.*
    FROM space s
    WHERE NOT EXISTS (
        SELECT 1 FROM llm_decisions d
        WHERE d.criteria_hash = s.criterion_key AND d.user_signature = s.signature
    )
),
uncompiled AS MATERIALIZED (
    SELECT DISTINCT ON (s.criterion_key) s.criterion_key, s.criterion_text, s.depends_on
    FROM space s
    WHERE (SELECT rules FROM settings)
      AND NOT EXISTS (SELECT 1 FROM criterion_rules r WHERE r.criterion_key = s.criterion_key)
    ORDER BY s.criterion_key, s.criterion_text
),
totals AS (
    SELECT (SELECT count(*) FROM space)::bigint AS cohorts_total,
           (SELECT count(*) FROM uncached)::bigint AS cohorts_uncached,
           (SELECT count(DISTINCT criterion_key) FROM space)::bigint AS criteria_total,
           (SELECT count(*) FROM uncompiled)::bigint AS criteria_uncompiled,
           round(extract(epoch FROM clock_timestamp() - statement_timestamp()) * 1000)::integer AS ms
),
metrics AS (
    INSERT INTO pipeline_metrics (run_id, batch_id, workflow, stage, pairs_in, pairs_out, duration_ms, details)
    SELECT $4, NULL, 'B', 'warm_cache', t.cohorts_total, t.cohorts_uncached, t.ms,
           jsonb_build_object('banding', s.cohort_bands, 'criteria', t.criteria_total,
                              'criteria_uncompiled', t.criteria_uncompiled)
    FROM totals t, settings s
    RETURNING 1
)
SELECT s.config,
       -- The match model's latency from its own call log, for sizing requests (AD29): a line
       -- fitted to duration over output tokens, last 200 answered calls.
       (SELECT jsonb_build_object(
                   'model', s.config ->> 'llm_model_match', 'calls', count(*),
                   'l0_ms', round(regr_intercept(c.duration_ms, c.tokens_out)),
                   'ms_per_token', round(regr_slope(c.duration_ms, c.tokens_out)::numeric, 3),
                   'r2', round(regr_r2(c.duration_ms, c.tokens_out)::numeric, 3))
        FROM (SELECT l.duration_ms, l.tokens_out FROM llm_calls l
              WHERE l.model = s.config ->> 'llm_model_match' AND l.outcome = 'ok'
                AND l.duration_ms IS NOT NULL
              ORDER BY l.call_id DESC LIMIT 200) c) AS llm_latency,
       t.cohorts_total,
       t.cohorts_uncached,
       t.criteria_total,
       t.criteria_uncompiled,
       t.ms AS duration_ms,
       (SELECT count(*) FROM metrics) AS metrics_written,
       coalesce((SELECT jsonb_agg(jsonb_build_object(
                     'criterion_key', criterion_key, 'criterion_text', criterion_text,
                     'depends_on', depends_on, 'signature', signature)
                     ORDER BY criterion_key, signature)
                 FROM uncached), '[]'::jsonb) AS cohorts,
       coalesce((SELECT jsonb_agg(jsonb_build_object(
                     'criterion_key', criterion_key, 'criterion_text', criterion_text,
                     'depends_on', depends_on)
                     ORDER BY criterion_key)
                 FROM uncompiled), '[]'::jsonb) AS criteria
FROM totals t, settings s;
