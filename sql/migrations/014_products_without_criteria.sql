-- A product that publishes no eligibility criteria we can check matches nobody.
--
-- Why: a NULL threshold never filters (recall first: a misread mustn't hide a product). For a
-- product where nothing at all was extracted (Union Bank, Bank of Maharashtra and Chola's
-- TurboLoan on BankBazaar's table), that rule made every customer "eligible", so every batch
-- reported 100% of customers with an offer. Saying they qualify for a loan whose page states
-- no criteria isn't a match; such products stay in the catalogue but are never offered.
--
-- has_criteria is true when the product states at least one threshold (income, score, age),
-- an allowed employment, or a fuzzy or compilable criterion, the things a customer's facts can
-- be checked against. Criteria that depend on facts we don't have (salary account, employer,
-- work experience) are caveats and don't count. match_candidates() is the only hard filter
-- (Workflow B, the cohort step and the bench all call it), so the rule lives here once.

ALTER TABLE loan_products ADD COLUMN has_criteria boolean NOT NULL GENERATED ALWAYS AS (
    min_monthly_income IS NOT NULL
    OR min_credit_score IS NOT NULL
    OR min_age IS NOT NULL
    OR max_age IS NOT NULL
    OR cardinality(coalesce(allowed_employment, '{}')) > 0
    OR coalesce(soft_criteria @? '$[*] ? (@.class == "fuzzy" || @.class == "compilable")', false)
) STORED;

CREATE OR REPLACE FUNCTION match_candidates(p_batch uuid)
RETURNS TABLE (user_id uuid, product_id bigint, criteria_hash text, has_fuzzy boolean)
LANGUAGE sql STABLE AS $$
    WITH products AS (
        SELECT p.product_id, p.criteria_hash, p.has_fuzzy_criteria,
               p.min_monthly_income, p.min_credit_score, p.min_age, p.max_age, p.allowed_employment
        FROM loan_products p
        WHERE p.is_active AND p.has_criteria
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
