-- Phase 4 / Workflow B: the matching functions, and set-based referential integrity for matches.
--
-- 1. match_candidates(batch): the hard filter (S2) as one inlinable SQL function. Workflow B's
--    write step and cohort step, and the bench, all call it, so the rules exist exactly once.
--    A NULL threshold never filters (recall first). Compilable filter rules, such as a higher
--    minimum income for one employment type, apply only when their condition holds.
-- 2. band_label / fuzzy_signatures(batch, mode): cohort signatures (S3). A fuzzy criterion is
--    judged only on the facts it depends on, so pairs with equal banded facts share a verdict.
-- 3. matches drops its three row-level foreign keys for statement-level checks. Measured at
--    100k users (2.37M rows): row-level FKs cost about 5 µs per row per key, 51 s in total; one
--    anti-join over the statement's transition table brings it to about 12 s with the same
--    guarantee on insert and update (docs/EXPERIMENTS.md, write path). Deletes keep the old
--    semantics: removing users cascades to their matches, while removing a product or batch
--    that matches still reference is refused.

-- ---------------------------------------------------------------------------------------------
-- 1. Hard filter
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION match_candidates(p_batch uuid)
RETURNS TABLE (user_id uuid, product_id bigint, criteria_hash text, has_fuzzy boolean)
LANGUAGE sql STABLE AS $$
    WITH products AS (
        SELECT p.product_id, p.criteria_hash, p.has_fuzzy_criteria,
               p.min_monthly_income, p.min_credit_score, p.min_age, p.max_age, p.allowed_employment
        FROM loan_products p
        WHERE p.is_active
    ),
    filter_rules AS (
        SELECT p.product_id,
               r -> 'rule' ->> 'field' AS field,
               r -> 'rule' ->> 'op' AS op,
               (r -> 'rule' ->> 'value')::numeric AS value,
               ARRAY(SELECT jsonb_array_elements_text(r -> 'rule' -> 'when' -> 'employment_status'))
                   AS when_employment
        FROM loan_products p, jsonb_array_elements(p.soft_criteria) AS r
        WHERE p.is_active
          AND r ->> 'class' = 'compilable'
          AND r ? 'rule'
          AND coalesce(r -> 'rule' ->> 'effect', 'filter') = 'filter'
    )
    SELECT u.user_id, p.product_id, p.criteria_hash, p.has_fuzzy_criteria
    FROM users u
    JOIN products p
      ON (p.min_monthly_income IS NULL OR u.monthly_income >= p.min_monthly_income)
     AND (p.min_credit_score IS NULL OR u.credit_score >= p.min_credit_score)
     AND (p.min_age IS NULL OR u.age >= p.min_age)
     AND (p.max_age IS NULL OR u.age <= p.max_age)
     AND (p.allowed_employment IS NULL OR u.employment_status = ANY (p.allowed_employment))
    WHERE u.batch_id = p_batch
      AND NOT EXISTS (
          SELECT 1
          FROM filter_rules fr
          WHERE fr.product_id = p.product_id
            AND (cardinality(fr.when_employment) = 0 OR u.employment_status = ANY (fr.when_employment))
            AND NOT CASE fr.op
                        WHEN '>=' THEN CASE fr.field
                                           WHEN 'monthly_income' THEN u.monthly_income >= fr.value
                                           WHEN 'credit_score' THEN u.credit_score >= fr.value
                                           WHEN 'age' THEN u.age >= fr.value
                                       END
                        WHEN '<=' THEN CASE fr.field
                                           WHEN 'monthly_income' THEN u.monthly_income <= fr.value
                                           WHEN 'credit_score' THEN u.credit_score <= fr.value
                                           WHEN 'age' THEN u.age <= fr.value
                                       END
                    END
      )
$$;

-- ---------------------------------------------------------------------------------------------
-- 2. Cohort signatures
-- ---------------------------------------------------------------------------------------------
-- The band a value falls in under a banding mode, clipped to the product's own allowed range
-- [p_lo, p_hi] (NULL = open), written as "lo..hi" with open ends left blank.
--   threshold  the product's own range only: every passing user shares one band per field.
--   coarse     income 25k / 50k / 100k, score 650 / 700 / 750, age 25 / 35 / 45.
--   fine       income in 10k steps (200k+ open), score in 25s, age in 5-year steps.
--   exact      the value itself (one cohort per distinct value).
--
-- Written as one expression with no FROM or CTE, so the planner inlines it into the calling
-- query. A version with a CTE ran as a separate function call per value: 15 s for 500k
-- pending pairs at 100k users. p_field is a literal at every call site, so the grid arrays
-- fold to constants. Out-of-range array subscripts return NULL, which is an open end.
CREATE FUNCTION band_label(p_field text, p_value numeric, p_lo numeric, p_hi numeric, p_mode text)
RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_mode = 'exact' THEN trunc(p_value)::bigint::text
        ELSE
            coalesce(trunc(greatest(
                CASE p_mode
                    WHEN 'coarse' THEN
                        (CASE p_field WHEN 'monthly_income' THEN '{25000,50000,100000}'::numeric[]
                                      WHEN 'credit_score' THEN '{650,700,750}'::numeric[]
                                      ELSE '{25,35,45}'::numeric[] END)
                        [width_bucket(p_value,
                            CASE p_field WHEN 'monthly_income' THEN '{25000,50000,100000}'::numeric[]
                                         WHEN 'credit_score' THEN '{650,700,750}'::numeric[]
                                         ELSE '{25,35,45}'::numeric[] END)]
                    WHEN 'fine' THEN
                        CASE WHEN p_field = 'monthly_income' AND p_value >= 200000 THEN 200000
                             ELSE floor(p_value / CASE p_field WHEN 'monthly_income' THEN 10000
                                                               WHEN 'credit_score' THEN 25
                                                               ELSE 5 END)
                                  * CASE p_field WHEN 'monthly_income' THEN 10000
                                                 WHEN 'credit_score' THEN 25
                                                 ELSE 5 END
                        END
                END,
                p_lo))::bigint::text, '')
            || '..'
            || coalesce(trunc(least(
                CASE p_mode
                    WHEN 'coarse' THEN
                        (CASE p_field WHEN 'monthly_income' THEN '{25000,50000,100000}'::numeric[]
                                      WHEN 'credit_score' THEN '{650,700,750}'::numeric[]
                                      ELSE '{25,35,45}'::numeric[] END)
                        [width_bucket(p_value,
                            CASE p_field WHEN 'monthly_income' THEN '{25000,50000,100000}'::numeric[]
                                         WHEN 'credit_score' THEN '{650,700,750}'::numeric[]
                                         ELSE '{25,35,45}'::numeric[] END) + 1] - 1
                    WHEN 'fine' THEN
                        CASE WHEN p_field = 'monthly_income' AND p_value >= 200000 THEN NULL
                             ELSE floor(p_value / CASE p_field WHEN 'monthly_income' THEN 10000
                                                               WHEN 'credit_score' THEN 25
                                                               ELSE 5 END)
                                  * CASE p_field WHEN 'monthly_income' THEN 10000
                                                 WHEN 'credit_score' THEN 25
                                                 ELSE 5 END
                                  + CASE p_field WHEN 'monthly_income' THEN 10000
                                                 WHEN 'credit_score' THEN 25
                                                 ELSE 5 END - 1
                        END
                END,
                p_hi))::bigint::text, '')
    END
$$;

-- One row per (pending pair, fuzzy criterion). criterion_key identifies the criterion by its
-- wording and dependencies, not by product, so products that share a criterion share cached
-- verdicts. signature lists the banded facts the criterion depends on, in field order.
CREATE FUNCTION fuzzy_signatures(p_batch uuid, p_mode text)
RETURNS TABLE (user_id uuid, product_id bigint, criterion_key text, criterion_text text,
               depends_on text[], signature text)
LANGUAGE sql STABLE AS $$
    WITH criteria AS (
        SELECT p.product_id, p.min_monthly_income, p.min_credit_score, p.min_age, p.max_age,
               c ->> 'text' AS criterion_text,
               ARRAY(SELECT d FROM jsonb_array_elements_text(c -> 'depends_on') AS d ORDER BY d)
                   AS depends_on
        FROM loan_products p, jsonb_array_elements(p.soft_criteria) AS c
        WHERE p.is_active AND c ->> 'class' = 'fuzzy'
    ),
    -- The key is hashed once per criterion here, not once per (pair, criterion) row below.
    fuzzy AS MATERIALIZED (
        SELECT c.*,
               encode(sha256(convert_to(lower(btrim(c.criterion_text)) || '|'
                                        || array_to_string(c.depends_on, ','), 'UTF8')), 'hex')
                   AS criterion_key
        FROM criteria c
    )
    SELECT m.user_id, m.product_id,
           f.criterion_key,
           f.criterion_text,
           f.depends_on,
           concat_ws(';',
               CASE WHEN 'age' = ANY (f.depends_on)
                    THEN 'age=' || band_label('age', u.age, f.min_age, f.max_age, p_mode) END,
               CASE WHEN 'credit_score' = ANY (f.depends_on)
                    THEN 'credit_score=' || band_label('credit_score', u.credit_score,
                                                       f.min_credit_score, NULL, p_mode) END,
               CASE WHEN 'employment_status' = ANY (f.depends_on)
                    THEN 'employment_status=' || u.employment_status END,
               CASE WHEN 'monthly_income' = ANY (f.depends_on)
                    THEN 'monthly_income=' || band_label('monthly_income', u.monthly_income,
                                                         f.min_monthly_income, NULL, p_mode) END)
    FROM match_candidates(p_batch) m
    JOIN fuzzy f USING (product_id)
    JOIN users u USING (user_id)
    WHERE m.has_fuzzy
$$;

-- ---------------------------------------------------------------------------------------------
-- 3. Set-based referential integrity for matches
-- ---------------------------------------------------------------------------------------------
ALTER TABLE matches
    DROP CONSTRAINT matches_user_id_fkey,
    DROP CONSTRAINT matches_product_id_fkey,
    DROP CONSTRAINT matches_batch_id_fkey;

CREATE FUNCTION matches_check_refs() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM new_rows n
               WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = n.user_id))
    OR EXISTS (SELECT 1 FROM new_rows n
               WHERE NOT EXISTS (SELECT 1 FROM loan_products p WHERE p.product_id = n.product_id))
    OR EXISTS (SELECT 1 FROM new_rows n
               WHERE NOT EXISTS (SELECT 1 FROM ingestion_batches b WHERE b.batch_id = n.batch_id))
    THEN
        RAISE EXCEPTION 'matches: reference to a missing user, product or batch'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER matches_refs_insert AFTER INSERT ON matches
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION matches_check_refs();
CREATE TRIGGER matches_refs_update AFTER UPDATE ON matches
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION matches_check_refs();

-- Deleting users removes their matches (the old ON DELETE CASCADE).
CREATE FUNCTION matches_cascade_users() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM matches m USING old_rows o WHERE m.user_id = o.user_id;
    RETURN NULL;
END
$$;
CREATE TRIGGER users_cascade_matches AFTER DELETE ON users
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION matches_cascade_users();

-- Deleting a product or batch that matches still reference is refused (the old RESTRICT).
CREATE FUNCTION matches_restrict_parent() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_TABLE_NAME = 'loan_products'
        AND EXISTS (SELECT 1 FROM matches m JOIN old_rows o USING (product_id)))
    OR (TG_TABLE_NAME = 'ingestion_batches'
        AND EXISTS (SELECT 1 FROM matches m JOIN old_rows o USING (batch_id)))
    THEN
        RAISE EXCEPTION '%: rows are still referenced by matches', TG_TABLE_NAME
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER loan_products_restrict_matches AFTER DELETE ON loan_products
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION matches_restrict_parent();
CREATE TRIGGER ingestion_batches_restrict_matches AFTER DELETE ON ingestion_batches
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION matches_restrict_parent();
