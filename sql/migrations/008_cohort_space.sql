-- Every cohort the active products can produce, enumerated from the products alone
-- (docs/EXPERIMENTS.md section 8, DECISIONS AD25).
--
-- fuzzy_signatures() derives cohorts from the users of one batch, so the first batch after a
-- product change waits for the LLM. cohort_space() lists the same (criterion, signature) pairs
-- without any users, so the "Warm match cache" sub-workflow can judge them as soon as Workflow A
-- has upserted the products. Under threshold banding the space is exactly what batches produce
-- (19 cohorts for the synthetic products, at 1k and at 100k users), so warming costs no extra
-- calls. The signature is fuzzy_signatures' own expression applied to every value a user who
-- passes the product could have; tests/test_match_sql.py checks that every signature a batch
-- produces is in this set.
--
-- Fact domains are the users table's CHECK constraints: age 18..100, credit score 300..900,
-- monthly income above 0. Income is sampled at the product's minimum and then every ₹1,000 up
-- to ₹2,50,000: every band boundary of every mode is a multiple of ₹1,000, and the fine bands
-- are open above ₹2,00,000, so these samples reach every label. 'exact' banding has no finite
-- space and gets no rows.
CREATE FUNCTION cohort_space(p_mode text)
RETURNS TABLE (criterion_key text, criterion_text text, depends_on text[], signature text)
LANGUAGE sql STABLE AS $$
    WITH criteria AS (
        SELECT p.min_monthly_income, p.min_credit_score, p.min_age, p.max_age,
               p.allowed_employment,
               c ->> 'text' AS criterion_text,
               ARRAY(SELECT d FROM jsonb_array_elements_text(c -> 'depends_on') AS d ORDER BY d)
                   AS depends_on
        FROM loan_products p, jsonb_array_elements(p.soft_criteria) AS c
        WHERE p.is_active AND c ->> 'class' = 'fuzzy' AND p_mode <> 'exact'
    ),
    fuzzy AS MATERIALIZED (
        SELECT c.*,
               encode(sha256(convert_to(lower(btrim(c.criterion_text)) || '|'
                                        || array_to_string(c.depends_on, ','), 'UTF8')), 'hex')
                   AS criterion_key
        FROM criteria c
    )
    SELECT DISTINCT f.criterion_key, f.criterion_text, f.depends_on,
           concat_ws(';', age.label, score.label, employment.label, income.label)
    FROM fuzzy f
    CROSS JOIN LATERAL (
        SELECT DISTINCT 'age=' || band_label('age', v, f.min_age, f.max_age, p_mode) AS label
        FROM generate_series(greatest(18, f.min_age), least(100, f.max_age)) AS v
        WHERE 'age' = ANY (f.depends_on)
        UNION ALL
        SELECT NULL WHERE NOT 'age' = ANY (f.depends_on)
    ) age
    CROSS JOIN LATERAL (
        SELECT DISTINCT 'credit_score=' || band_label('credit_score', v, f.min_credit_score, NULL,
                                                      p_mode) AS label
        FROM generate_series(greatest(300, f.min_credit_score), 900) AS v
        WHERE 'credit_score' = ANY (f.depends_on)
        UNION ALL
        SELECT NULL WHERE NOT 'credit_score' = ANY (f.depends_on)
    ) score
    CROSS JOIN LATERAL (
        SELECT 'employment_status=' || e AS label
        FROM unnest(coalesce(f.allowed_employment,
                             ARRAY['Business', 'Salaried', 'Self-Employed'])) AS e
        WHERE 'employment_status' = ANY (f.depends_on)
        UNION ALL
        SELECT NULL WHERE NOT 'employment_status' = ANY (f.depends_on)
    ) employment
    CROSS JOIN LATERAL (
        SELECT DISTINCT 'monthly_income=' || band_label('monthly_income', v, f.min_monthly_income,
                                                        NULL, p_mode) AS label
        FROM (SELECT generate_series(1000, 250000, 1000)::numeric AS v
              UNION
              SELECT f.min_monthly_income) AS samples
        WHERE 'monthly_income' = ANY (f.depends_on)
          AND v > 0 AND v >= coalesce(f.min_monthly_income, 0)
        UNION ALL
        SELECT NULL WHERE NOT 'monthly_income' = ANY (f.depends_on)
    ) income
$$;
