-- Workflow B, S4 + S5: cache the LLM's answers and record what the calls cost.
-- A cohort verdict is keyed by (criterion_key, signature), so it serves every user in the
-- cohort, in this batch and every later one, until the criterion's wording changes. A compiled
-- rule is keyed by criterion_key alone and serves every product that shares the criterion.
--
-- Parameters: #1 decisions (JSON array from parse_cohort_verdicts.js), #2 batch_id,
--             #3 execution id, #4 stats JSON {calls, tokens_in, tokens_out, requests,
--             failed_requests, unanswered}, #5 rules (JSON array from parse_cohort_verdicts.js)
WITH d AS (
    SELECT *
    FROM jsonb_to_recordset($1::jsonb) AS x (
        criterion_key text, signature text, verdict text, reason text, model text,
        prompt_version text)
),
stored AS (
    INSERT INTO llm_decisions (criteria_hash, user_signature, verdict, reason, model, prompt_version)
    SELECT criterion_key, signature, verdict, reason, model, coalesce(prompt_version, 'unknown')
    FROM d
    ON CONFLICT (criteria_hash, user_signature) DO NOTHING
    RETURNING 1
),
r AS (
    SELECT *
    FROM jsonb_to_recordset(coalesce($5, '[]')::jsonb) AS x (
        criterion_key text, criterion_text text, depends_on text[], clauses jsonb, note text,
        model text, prompt_version text)
),
rules_stored AS (
    INSERT INTO criterion_rules (criterion_key, criterion_text, depends_on, clauses, note, model,
                                 prompt_version)
    SELECT criterion_key, criterion_text, depends_on, clauses, note, model,
           coalesce(prompt_version, 'unknown')
    FROM r
    ON CONFLICT (criterion_key) DO NOTHING
    RETURNING 1
),
stats AS (
    SELECT * FROM jsonb_to_record($4::jsonb) AS s (
        calls integer, tokens_in integer, tokens_out integer, requests integer,
        failed_requests integer, unanswered integer)
),
metrics AS (
    INSERT INTO pipeline_metrics (run_id, batch_id, workflow, stage, pairs_in, pairs_out, llm_calls,
                                  tokens_in, tokens_out, details)
    SELECT $3, $2::uuid, 'B', 's5_llm',
           (SELECT count(*) FROM d) + s.unanswered, (SELECT count(*) FROM d),
           s.calls, s.tokens_in, s.tokens_out,
           jsonb_build_object('requests', s.requests, 'failed_requests', s.failed_requests,
                              'unanswered', s.unanswered,
                              'rules', (SELECT count(*) FROM r),
                              'rules_stored', (SELECT count(*) FROM rules_stored))
    FROM stats s
    RETURNING 1
)
SELECT (SELECT count(*) FROM d) AS decisions,
       (SELECT count(*) FROM stored) AS stored,
       (SELECT count(*) FROM rules_stored) AS rules_stored,
       (SELECT count(*) FROM metrics) AS metrics_written;
