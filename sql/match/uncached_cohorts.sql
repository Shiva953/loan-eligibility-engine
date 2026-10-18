-- Workflow B, first statement of a batch: claim it, read app_config, and find the questions
-- the LLM still has to answer (S3 + S4). One round trip instead of three (docs/DECISIONS.md
-- AD27: every statement costs a Bengaluru-Sydney round trip when n8n runs on a laptop).
--
-- * Claim: only one run can move a batch from 'ingested' (or a 'matching' run stuck for 30+
--   minutes) to 'matching', so a replayed webhook and the sweeper can't match it twice. When
--   the claim fails, nothing else happens: no cohorts, no metrics.
-- * Settings: an empty banding mode, match_llm_enabled or match_rules_enabled means "read it
--   from app_config" (Workflow B). The resolved values come back, and Write matches uses
--   exactly these, so one run never mixes two settings. The bench and tests pass explicit
--   values and claim = 'false' to compare variants on an unclaimed batch.
-- * Cohorts: every pending (pair, fuzzy criterion) collapses to a cohort (criterion_key,
--   banded signature). Cohorts that already have a verdict in llm_decisions are answered from
--   the cache. Only the rest go to n8n: one row with a JSON array whose length is the number
--   of uncached cohorts, never the number of users. With the rule judge on, the batch's
--   criteria that have no compiled rule yet come back too (at most one per distinct
--   criterion). The stage counts go into pipeline_metrics in the same statement.
-- * The banding mode must reach fuzzy_signatures() as a literal: the planner then inlines the
--   function and folds band_label() for that mode once, instead of on every row (passing it as
--   a column cost 25% at 100k users). So sig has one branch per mode, and a one-time filter
--   runs only the configured branch; threshold also takes any other value, as band_label does.
--
-- Parameters: #1 batch_id, #2 banding mode, #3 match_llm_enabled, #4 execution id,
--             #5 match_rules_enabled ('' = from app_config for #2, #3, #5),
--             #6 claim the batch first ('true' in Workflow B, 'false' in the bench and tests)
--
-- JIT is off for this statement: at 100k users the estimate passes jit_above_cost, and compiling
-- cost more than it saved (800 -> 670 ms without it). n8n sends both statements as one message,
-- so SET LOCAL ends with it (DECISIONS V23).
SET LOCAL jit = off;

WITH config AS (
    SELECT jsonb_object_agg(key, value) AS config FROM app_config
),
settings AS (
    SELECT c.config,
           coalesce(nullif($2, ''), c.config ->> 'cohort_bands') AS cohort_bands,
           coalesce(nullif($3, ''), c.config ->> 'match_llm_enabled')::boolean AS llm,
           coalesce(nullif($5, ''), c.config ->> 'match_rules_enabled')::boolean AS rules
    FROM config c
),
claimed AS (
    UPDATE ingestion_batches
    SET status = 'matching', matching_at = now(), error = NULL
    WHERE $6::boolean
      AND batch_id = $1::uuid
      AND (status = 'ingested'
           OR (status = 'matching' AND matching_at < now() - interval '30 minutes'))
    RETURNING batch_id
),
go AS (
    SELECT NOT $6::boolean OR EXISTS (SELECT 1 FROM claimed) AS go
),
sig AS MATERIALIZED (
    SELECT f.criterion_key, f.criterion_text, f.depends_on, f.signature, count(*) AS pairs
    FROM (
        SELECT * FROM fuzzy_signatures($1::uuid, 'threshold')
        WHERE (SELECT cohort_bands NOT IN ('coarse', 'fine', 'exact') FROM settings)
        UNION ALL
        SELECT * FROM fuzzy_signatures($1::uuid, 'coarse')
        WHERE (SELECT cohort_bands = 'coarse' FROM settings)
        UNION ALL
        SELECT * FROM fuzzy_signatures($1::uuid, 'fine')
        WHERE (SELECT cohort_bands = 'fine' FROM settings)
        UNION ALL
        SELECT * FROM fuzzy_signatures($1::uuid, 'exact')
        WHERE (SELECT cohort_bands = 'exact' FROM settings)
    ) f
    WHERE (SELECT llm FROM settings) AND (SELECT go FROM go)
    GROUP BY f.criterion_key, f.criterion_text, f.depends_on, f.signature
),
uncached AS MATERIALIZED (
    SELECT s.*
    FROM sig s
    WHERE NOT EXISTS (
        SELECT 1 FROM llm_decisions d
        WHERE d.criteria_hash = s.criterion_key AND d.user_signature = s.signature
    )
),
uncompiled AS MATERIALIZED (
    SELECT DISTINCT s.criterion_key, s.criterion_text, s.depends_on
    FROM sig s
    WHERE (SELECT rules FROM settings)
      AND NOT EXISTS (SELECT 1 FROM criterion_rules r WHERE r.criterion_key = s.criterion_key)
),
totals AS (
    SELECT (SELECT coalesce(sum(pairs), 0) FROM sig)::bigint AS pending_rows,
           (SELECT count(*) FROM sig)::bigint AS cohorts_total,
           (SELECT count(*) FROM uncached)::bigint AS cohorts_uncached,
           (SELECT count(DISTINCT criterion_key) FROM sig)::bigint AS criteria_total,
           (SELECT count(*) FROM uncompiled)::bigint AS criteria_uncompiled,
           round(extract(epoch FROM clock_timestamp() - statement_timestamp()) * 1000)::integer AS ms
),
metrics AS (
    INSERT INTO pipeline_metrics (run_id, batch_id, workflow, stage, pairs_in, pairs_out, duration_ms, details)
    SELECT $4, $1::uuid, 'B', stage, pairs_in, pairs_out, t.ms,
           jsonb_build_object('banding', s.cohort_bands, 'criteria', t.criteria_total,
                              'criteria_uncompiled', t.criteria_uncompiled)
    FROM totals t, settings s, go g,
         LATERAL (VALUES ('s3_cohorts', t.pending_rows, t.cohorts_total),
                         ('s4_cache', t.cohorts_total, t.cohorts_uncached)) AS v (stage, pairs_in, pairs_out)
    WHERE g.go
    RETURNING 1
)
SELECT $1::text AS batch_id,
       g.go AS claimed,
       (SELECT status FROM ingestion_batches WHERE batch_id = $1::uuid) AS status_before,
       s.cohort_bands,
       s.llm::text AS match_llm_enabled,
       s.rules::text AS match_rules_enabled,
       s.config,
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
       t.pending_rows,
       t.cohorts_total,
       t.cohorts_uncached,
       t.criteria_uncompiled,
       t.ms AS duration_ms,
       (SELECT count(*) FROM metrics) AS metrics_written,
       coalesce((SELECT jsonb_agg(jsonb_build_object(
                     'criterion_key', criterion_key, 'criterion_text', criterion_text,
                     'depends_on', depends_on, 'signature', signature, 'pairs', pairs)
                     ORDER BY criterion_key, signature)
                 FROM uncached), '[]'::jsonb) AS cohorts,
       coalesce((SELECT jsonb_agg(jsonb_build_object(
                     'criterion_key', criterion_key, 'criterion_text', criterion_text,
                     'depends_on', depends_on)
                     ORDER BY criterion_key)
                 FROM uncompiled), '[]'::jsonb) AS criteria
FROM totals t, settings s, go g;
