-- Workflow A: upsert one source's validated products in a single statement, and record a
-- criteria diff for every product that is new or whose criteria changed.
--
-- Parameters:
--   #1  products: JSON array produced by n8n/code/validate_products.js
--   #2  source_id
--   #3  crawl run id (links the history rows to the run that saw the change)
--   #4  content hash of the cleaned page text
--
-- Every CTE sees the same snapshot, so `previous` holds the criteria from before this upsert.
-- A product whose criteria change gets needs_review = true until someone clears it. A first
-- sighting writes a history row with old_criteria NULL, so every product has a starting point
-- for later diffs.
--
-- Products are matched on their identity within the source (lender + name, normalised:
-- migration 010), not the exact wording, so a re-extraction that names a product differently
-- updates it (and takes the new name) instead of adding a copy. Two entries of one extraction
-- with the same identity are one product: the one with more hard-filter fields is kept, as
-- validate_products.js does for exact repeats.
WITH extracted AS (
    SELECT p.*, product_identity(p.lender, p.product_name) AS identity_key, e.ord
    FROM jsonb_array_elements($1::jsonb) WITH ORDINALITY AS e (item, ord)
    CROSS JOIN LATERAL jsonb_to_record(e.item) AS p (
        lender              text,
        product_name        text,
        source_url          text,
        interest_rate_min   numeric,
        interest_rate_max   numeric,
        min_monthly_income  numeric,
        min_credit_score    smallint,
        min_age             smallint,
        max_age             smallint,
        allowed_employment  text[],
        soft_criteria       jsonb,
        field_confidence    jsonb,
        criteria_json       jsonb,
        criteria_hash       text
    )
),
incoming AS (
    SELECT DISTINCT ON (identity_key) e.*
    FROM extracted e
    ORDER BY identity_key,
             num_nonnulls(min_age, max_age, min_credit_score, min_monthly_income,
                          allowed_employment, interest_rate_min) DESC,
             ord
),
previous AS (
    SELECT lp.identity_key, lp.criteria_json, lp.criteria_hash
    FROM loan_products lp
    JOIN incoming i ON lp.source_id = $2::integer AND lp.identity_key = i.identity_key
),
upserted AS (
    INSERT INTO loan_products AS lp (
        source_id, source_url, lender, product_name,
        interest_rate_min, interest_rate_max, min_monthly_income, min_credit_score,
        min_age, max_age, allowed_employment, soft_criteria, field_confidence,
        criteria_json, criteria_hash, content_hash, is_active, last_seen_at
    )
    SELECT $2::integer, source_url, lender, product_name,
           interest_rate_min, interest_rate_max, min_monthly_income, min_credit_score,
           min_age, max_age, allowed_employment, soft_criteria, field_confidence,
           criteria_json, criteria_hash, $4, true, now()
    FROM incoming
    ON CONFLICT (source_id, identity_key) DO UPDATE SET
        source_url         = EXCLUDED.source_url,
        lender             = EXCLUDED.lender,
        product_name       = EXCLUDED.product_name,
        interest_rate_min  = EXCLUDED.interest_rate_min,
        interest_rate_max  = EXCLUDED.interest_rate_max,
        min_monthly_income = EXCLUDED.min_monthly_income,
        min_credit_score   = EXCLUDED.min_credit_score,
        min_age            = EXCLUDED.min_age,
        max_age            = EXCLUDED.max_age,
        allowed_employment = EXCLUDED.allowed_employment,
        soft_criteria      = EXCLUDED.soft_criteria,
        field_confidence   = EXCLUDED.field_confidence,
        criteria_json      = EXCLUDED.criteria_json,
        needs_review       = lp.needs_review OR lp.criteria_hash <> EXCLUDED.criteria_hash,
        criteria_hash      = EXCLUDED.criteria_hash,
        content_hash       = EXCLUDED.content_hash,
        is_active          = true,
        last_seen_at       = now()
    RETURNING lp.product_id, lp.identity_key, lp.criteria_json,
              lp.criteria_hash, (lp.xmax = 0) AS inserted
),
-- Products of this source that the new extraction did not return are retired at once. Their
-- criteria predate the page as it reads now, and keeping them active kept stale, often looser,
-- rules matching customers until the stale-days sweep (docs/DECISIONS.md AD37). An empty
-- extraction retires nothing, so a failed read can't empty a source.
retired AS (
    UPDATE loan_products lp
    SET is_active = false
    WHERE lp.source_id = $2::integer
      AND lp.is_active
      AND EXISTS (SELECT 1 FROM incoming)
      AND NOT EXISTS (SELECT 1 FROM incoming i WHERE i.identity_key = lp.identity_key)
    RETURNING lp.product_id
),
history AS (
    INSERT INTO product_criteria_history (product_id, crawl_run_id, old_criteria, new_criteria, changed_fields)
    SELECT u.product_id,
           $3::bigint,
           p.criteria_json,
           u.criteria_json,
           ARRAY(
               SELECT k
               FROM (
                   SELECT jsonb_object_keys(u.criteria_json) AS k
                   UNION
                   SELECT jsonb_object_keys(COALESCE(p.criteria_json, '{}'::jsonb))
               ) AS keys
               WHERE (u.criteria_json -> k) IS DISTINCT FROM (p.criteria_json -> k)
               ORDER BY k
           )
    FROM upserted u
    LEFT JOIN previous p USING (identity_key)
    WHERE p.criteria_hash IS DISTINCT FROM u.criteria_hash
    RETURNING product_id, old_criteria IS NULL AS first_sighting
)
SELECT (SELECT count(*) FROM upserted)                                   AS products_upserted,
       (SELECT count(*) FROM upserted WHERE inserted)                    AS products_new,
       (SELECT count(*) FROM history WHERE NOT first_sighting)           AS products_changed,
       (SELECT count(*) FROM retired)                                    AS products_retired;
