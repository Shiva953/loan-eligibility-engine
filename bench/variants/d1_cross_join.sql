-- D1: cross join. Every batch user is checked against every active product in one join:
-- N × M rule evaluations inside Postgres, with nothing leaving the database.
-- Parameter: $1 batch_id. Returns one row per (user, product) that passes the hard rules.
WITH products AS (
    SELECT product_id, criteria_hash, has_fuzzy_criteria,
           min_monthly_income, min_credit_score, min_age, max_age, allowed_employment
    FROM loan_products
    WHERE is_active AND has_criteria
),
-- Compilable filter rules (for example a higher minimum income for one employment type),
-- unpacked from jsonb once per product rather than once per pair.
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
SELECT u.user_id, p.product_id, p.criteria_hash,
       CASE WHEN p.has_fuzzy_criteria THEN 'pending_llm' ELSE 'matched' END AS status
FROM users u
JOIN products p
  ON (p.min_monthly_income IS NULL OR u.monthly_income >= p.min_monthly_income)
 AND (p.min_credit_score IS NULL OR u.credit_score >= p.min_credit_score)
 AND (p.min_age IS NULL OR u.age >= p.min_age)
 AND (p.max_age IS NULL OR u.age <= p.max_age)
 AND (p.allowed_employment IS NULL OR u.employment_status = ANY (p.allowed_employment))
WHERE u.batch_id = $1
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
