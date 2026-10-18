-- Workflow B: write every match of the batch once, with its final status (decide before write).
--
-- The hard filter (match_candidates) and the cached cohort verdicts are joined in one INSERT,
-- so each pair is written exactly once. Writing pending rows and then updating them after the
-- LLM would write every fuzzy pair twice, and the write is the expensive part (2.37M rows at
-- 100k users).
--
-- Write path (docs/EXPERIMENTS.md sections 5 and 12):
--   * One row per user in user_matches (DECISIONS AD31): the user's pairs as arrays sorted by
--     product id, and the few pair details in a map. A 10k batch writes 10k rows instead of
--     214k pairs (1.74 s -> 0.41 s on the real products; a re-upload 4.13 s -> 0.61 s).
--   * Users with no row yet take a plain INSERT. Only users who already have one (a re-upload,
--     or a rerun of this batch) go through ON CONFLICT DO UPDATE. If another batch writes the
--     same user between this statement's snapshot and its insert, the plain INSERT fails on the
--     primary key and rolls back whole; the node retries once, and the retry sees that row and
--     takes the upsert path.
--   * A returning user whose decisions are unchanged is skipped before ON CONFLICT, so a
--     replayed batch writes nothing and nobody is re-notified.
--   * to_announce marks users with offers (matched or llm_matched) that no email announced yet:
--     every user the write touches, since its decisions or its batch changed.
--   * Rows are inserted pre-sorted by the primary key, which keeps index maintenance local.
--   * work_mem is raised for this statement only, so that sort stays in memory at 10k users
--     (the default 4 MB spilled 23 MB to disk). n8n's Postgres node sends the formatted text
--     as one simple-protocol message, so SET LOCAL and the INSERT share an implicit
--     transaction and the setting ends with it.
--   * rank_rules is materialised (a handful of rows), so scores touches only the pairs of
--     products that have a ranking rule.
--
-- Each fuzzy criterion has two judges (docs/EXPERIMENTS.md, LLM stage): the cohort verdict
-- (S3-S5) and, when match_rules_enabled, the criterion's compiled rule applied to the user's
-- exact facts. They vote; see the voted CTE.
--
-- Status per pair:
--   matched       passes the hard rules and has no fuzzy criterion (or the match-time LLM is
--                 switched off: fuzzy criteria then become caveats)
--   llm_matched   every fuzzy criterion voted eligible or uncertain ("may qualify"; an
--                 uncertain criterion is kept as a caveat on the row)
--   llm_rejected  at least one fuzzy criterion voted ineligible (kept as evidence)
--   pending_llm   some cohort has no verdict yet (the LLM failed or the budget is spent);
--                 a later run decides it
-- Product-level caveats (unknown_field criteria) are not copied into each row: they are read
-- from loan_products when the email is built, which keeps rows narrow.
--
-- The same statement closes the batch (matching -> matched), so writing and finishing are one
-- round trip (docs/DECISIONS.md AD27). A batch that was not claimed (the bench and tests) is
-- left as it is.
--
-- Parameters: #1 batch_id, #2 banding mode, #3 match_llm_enabled ('true'/'false'),
--             #4 execution id, #5 match_rules_enabled ('true'/'false')
SET LOCAL work_mem = '64MB';
-- JIT made no difference to this statement, measured (1.61 s vs 1.63 s at 10k users, 26.9 s vs
-- 26.8 s at 100k), but its estimate sits right at jit_above_cost at 10k users, so a slightly
-- bigger batch would start paying the compile time for nothing.
SET LOCAL jit = off;

WITH cand AS MATERIALIZED (
    SELECT * FROM match_candidates($1::uuid)
),
-- The rule judge's clauses, one typed row per clause (a handful), so applying them is a join.
clauses AS MATERIALIZED (
    SELECT r.criterion_key,
           c ->> 'verdict' AS verdict,
           ARRAY(SELECT jsonb_array_elements_text(c -> 'employment_status')) AS employment,
           (c ->> 'age_min')::numeric AS age_min,
           (c ->> 'age_max')::numeric AS age_max,
           (c ->> 'credit_score_min')::numeric AS score_min,
           (c ->> 'credit_score_max')::numeric AS score_max,
           (c ->> 'monthly_income_min')::numeric AS income_min,
           (c ->> 'monthly_income_max')::numeric AS income_max
    FROM criterion_rules r, jsonb_array_elements(r.clauses) c
    WHERE $3::boolean AND $5::boolean
),
-- One row per (pair, fuzzy criterion) with both judges' votes. The cohort judge votes through
-- the cached verdict of the pair's cohort (NULL: not judged yet). The rule judge votes through
-- the clauses that cover the user's exact facts, and abstains (NULL) when none does or when
-- covering clauses disagree.
judged AS (
    SELECT s.user_id, s.product_id, s.criterion_key,
           min(s.criterion_text) AS criterion_text,
           min(d.verdict) AS cohort,
           min(d.reason) AS cohort_reason,
           CASE WHEN bool_or(k.verdict = 'ineligible') AND NOT bool_or(k.verdict = 'eligible')
                    THEN 'ineligible'
                WHEN bool_or(k.verdict = 'eligible') AND NOT bool_or(k.verdict = 'ineligible')
                    THEN 'eligible'
           END AS rule
    FROM fuzzy_signatures($1::uuid, $2) s
    JOIN users u ON u.user_id = s.user_id
    LEFT JOIN llm_decisions d
      ON d.criteria_hash = s.criterion_key AND d.user_signature = s.signature
    LEFT JOIN clauses k
      ON k.criterion_key = s.criterion_key
     AND (cardinality(k.employment) = 0 OR u.employment_status = ANY (k.employment))
     AND (k.age_min IS NULL OR u.age >= k.age_min) AND (k.age_max IS NULL OR u.age <= k.age_max)
     AND (k.score_min IS NULL OR u.credit_score >= k.score_min)
     AND (k.score_max IS NULL OR u.credit_score <= k.score_max)
     AND (k.income_min IS NULL OR u.monthly_income >= k.income_min)
     AND (k.income_max IS NULL OR u.monthly_income <= k.income_max)
    WHERE $3::boolean
    GROUP BY s.user_id, s.product_id, s.criterion_key
),
-- The vote. The cohort judge decides, and the compiled rule can only sharpen it: a clause that
-- rejects the user's exact facts turns an 'uncertain' cohort verdict into 'ineligible' (a veto).
-- A rule never vouches: its 'eligible' leaves an uncertain criterion uncertain, so the caveat
-- stays (compiled rules over-reach on what the facts can't show). Any disagreement makes the
-- criterion uncertain, so neither judge can drop a pair the other affirms. A pair is never
-- decided before its cohort has a verdict.
voted AS (
    SELECT j.*,
           CASE WHEN j.cohort IS NULL THEN NULL
                WHEN j.rule IS NULL OR j.rule = j.cohort THEN j.cohort
                WHEN j.cohort = 'uncertain' AND j.rule = 'ineligible' THEN 'ineligible'
                ELSE 'uncertain'
           END AS verdict
    FROM judged j
),
verdicts AS (
    SELECT user_id, product_id,
           bool_or(cohort IS NULL) AS undecided,
           bool_or(verdict = 'ineligible') AS rejected,
           array_agg('May not apply: ' || criterion_text) FILTER (WHERE verdict = 'uncertain')
               AS uncertain_notes,
           string_agg(criterion_text || ': ' || verdict
                      || coalesce(' (' || CASE WHEN verdict = cohort THEN nullif(cohort_reason, '')
                                              WHEN verdict = rule THEN 'compiled rule'
                                              ELSE 'the two judges disagree'
                                         END || ')', ''), '; ') AS reason
    FROM voted
    GROUP BY user_id, product_id
),
-- Rules-only mode: the fuzzy criteria are shown as caveats instead of being judged.
unjudged AS (
    SELECT p.product_id,
           array_agg('Not checked: ' || (c ->> 'text')) AS notes
    FROM loan_products p, jsonb_array_elements(p.soft_criteria) c
    WHERE p.is_active AND c ->> 'class' = 'fuzzy' AND NOT $3::boolean
    GROUP BY p.product_id
),
-- Ranking rules (for example a preferred credit score) never exclude anyone; they score.
rank_rules AS MATERIALIZED (
    SELECT p.product_id,
           r -> 'rule' ->> 'field' AS field,
           (r -> 'rule' ->> 'value')::numeric AS value
    FROM loan_products p, jsonb_array_elements(p.soft_criteria) r
    WHERE p.is_active AND r ->> 'class' = 'compilable' AND r -> 'rule' ->> 'effect' = 'rank'
),
scores AS (
    SELECT c.user_id, c.product_id,
           avg(CASE WHEN CASE rr.field
                             WHEN 'credit_score' THEN u.credit_score
                             WHEN 'monthly_income' THEN u.monthly_income
                             WHEN 'age' THEN u.age
                         END >= rr.value THEN 1.0 ELSE 0.0 END)::numeric(6, 3) AS score
    FROM cand c
    JOIN rank_rules rr USING (product_id)
    JOIN users u USING (user_id)
    GROUP BY c.user_id, c.product_id
),
statused AS (
    SELECT c.user_id, c.product_id, c.criteria_hash, sc.score,
           CASE WHEN NOT c.has_fuzzy OR NOT $3::boolean THEN 'matched'
                WHEN v.undecided THEN 'pending_llm'
                WHEN v.rejected THEN 'llm_rejected'
                ELSE 'llm_matched'
           END AS status,
           CASE WHEN c.has_fuzzy AND $3::boolean AND NOT v.undecided THEN v.reason END AS reason,
           coalesce(v.uncertain_notes, uj.notes, '{}') AS caveats
    FROM cand c
    LEFT JOIN verdicts v USING (user_id, product_id)
    LEFT JOIN scores sc USING (user_id, product_id)
    LEFT JOIN unjudged uj ON uj.product_id = c.product_id AND c.has_fuzzy
),
decided AS (
    SELECT s.*,
           CASE s.status WHEN 'matched' THEN 'rule' WHEN 'pending_llm' THEN NULL ELSE 'llm' END
               AS decided_by
    FROM statused s
),
-- One row per user: the user's pairs as arrays sorted by product, and the few pair details
-- (score, reason, caveats) in a map keyed by product id. decided_by follows from the status.
per_user AS (
    SELECT d.user_id,
           array_agg(d.product_id ORDER BY d.product_id) AS product_ids,
           array_agg(d.status ORDER BY d.product_id) AS statuses,
           coalesce(jsonb_object_agg(d.product_id::text, jsonb_strip_nulls(jsonb_build_object(
                        'score', d.score, 'reason', d.reason,
                        'caveats', CASE WHEN cardinality(d.caveats) > 0 THEN to_jsonb(d.caveats) END)))
                    FILTER (WHERE d.score IS NOT NULL OR d.reason IS NOT NULL
                                  OR cardinality(d.caveats) > 0),
                    '{}'::jsonb) AS details,
           bool_or(d.status IN ('matched', 'llm_matched')) AS has_offers
    FROM decided d
    GROUP BY d.user_id
),
returning_users AS MATERIALIZED (
    SELECT u.user_id
    FROM users u
    WHERE u.batch_id = $1::uuid
      AND EXISTS (SELECT 1 FROM user_matches x WHERE x.user_id = u.user_id)
),
inserted AS (
    INSERT INTO user_matches (user_id, batch_id, product_ids, statuses, details, to_announce)
    SELECT p.user_id, $1::uuid, p.product_ids, p.statuses, p.details, p.has_offers
    FROM per_user p
    WHERE NOT EXISTS (SELECT 1 FROM returning_users r WHERE r.user_id = p.user_id)
    ORDER BY p.user_id
    RETURNING cardinality(product_ids) AS pairs
),
upserted AS (
    INSERT INTO user_matches AS m (user_id, batch_id, product_ids, statuses, details, to_announce)
    SELECT p.user_id, $1::uuid, p.product_ids, p.statuses, p.details, p.has_offers
    FROM per_user p
    JOIN returning_users r USING (user_id)
    WHERE NOT EXISTS (
        SELECT 1 FROM user_matches x
        WHERE x.user_id = p.user_id
          AND (x.batch_id, x.product_ids, x.statuses, x.details)
              IS NOT DISTINCT FROM ($1::uuid, p.product_ids, p.statuses, p.details))
    ORDER BY p.user_id
    ON CONFLICT (user_id) DO UPDATE SET
        batch_id    = EXCLUDED.batch_id,
        product_ids = EXCLUDED.product_ids,
        statuses    = EXCLUDED.statuses,
        details     = EXCLUDED.details,
        to_announce = EXCLUDED.to_announce,
        updated_at  = now()
    WHERE (m.batch_id, m.product_ids, m.statuses, m.details)
          IS DISTINCT FROM (EXCLUDED.batch_id, EXCLUDED.product_ids, EXCLUDED.statuses, EXCLUDED.details)
    RETURNING cardinality(m.product_ids) AS pairs
),
done AS (
    SELECT coalesce(sum(pairs), 0)::bigint AS rows_written, count(*) AS users_written,
           round(extract(epoch FROM clock_timestamp() - statement_timestamp()) * 1000)::integer AS ms
    FROM (SELECT pairs FROM inserted UNION ALL SELECT pairs FROM upserted) w
),
finished AS (
    UPDATE ingestion_batches b
    SET status = 'matched', matched_at = clock_timestamp()
    FROM done d
    WHERE b.batch_id = $1::uuid AND b.status = 'matching'
    RETURNING b.matching_at, b.matched_at
),
summary AS (
    SELECT t.pairs, d.rows_written, d.users_written, t.matched, t.llm_matched, t.llm_rejected, t.pending_llm,
           (SELECT count(*) FROM users WHERE batch_id = $1::uuid) AS batch_users,
           (SELECT count(*) FROM loan_products WHERE is_active AND has_criteria) AS active_products,
           (SELECT count(*) FROM returning_users) AS returning_users,
           (SELECT count(*) FILTER (WHERE has_offers) FROM per_user) AS users_with_offers,
           d.ms
    FROM done d,
         (SELECT count(*) AS pairs,
                 count(*) FILTER (WHERE status = 'matched') AS matched,
                 count(*) FILTER (WHERE status = 'llm_matched') AS llm_matched,
                 count(*) FILTER (WHERE status = 'llm_rejected') AS llm_rejected,
                 count(*) FILTER (WHERE status = 'pending_llm') AS pending_llm
          FROM decided) t
),
metrics AS (
    INSERT INTO pipeline_metrics (run_id, batch_id, workflow, stage, pairs_in, pairs_out, duration_ms, details)
    SELECT $4, $1::uuid, 'B', v.stage, v.pairs_in, v.pairs_out, s.ms, v.details
    FROM summary s,
         LATERAL (VALUES
             ('s2_hard_filter', s.batch_users * s.active_products, s.pairs,
              jsonb_build_object('batch_users', s.batch_users, 'active_products', s.active_products)),
             ('write', s.pairs, s.rows_written,
              jsonb_build_object('matched', s.matched, 'llm_matched', s.llm_matched,
                                 'llm_rejected', s.llm_rejected, 'pending_llm', s.pending_llm,
                                 'returning_users', s.returning_users, 'users_written', s.users_written,
                                 'users_with_offers', s.users_with_offers,
                                 'unchanged', s.pairs - s.rows_written))
         ) AS v (stage, pairs_in, pairs_out, details)
    RETURNING 1
)
SELECT $1::text AS batch_id,
       EXISTS (SELECT 1 FROM finished) AS finished,
       (SELECT round(extract(epoch FROM matched_at - matching_at) * 1000)::integer FROM finished)
           AS matching_ms,
       s.*,
       (SELECT count(*) FROM metrics) AS metrics_written
FROM summary s;
