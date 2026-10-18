-- D2: decision cells. Every hard rule is a threshold test, so the thresholds of all active
-- products cut each field into intervals, and inside one interval every product's test gives
-- the same answer. A user's cell is (income interval, score interval, age interval,
-- employment). Users in the same cell are indistinguishable to every product, so the rules
-- run once per occupied cell (G × M evaluations, G ≤ N) and the answers are joined back to
-- the users. width_bucket finds each interval by binary search over the sorted cut points.
-- Parameter: $1 batch_id.
WITH products AS (
    SELECT product_id, criteria_hash, has_fuzzy_criteria,
           min_monthly_income, min_credit_score, min_age, max_age, allowed_employment
    FROM loan_products
    WHERE is_active AND has_criteria
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
),
-- Cut points per field. For ">= t" the cut is t; for "<= t" it is t + 1, so a value's
-- interval decides the test. The sentinel keeps width_bucket's array non-empty.
cuts AS (
    SELECT
        (SELECT array_agg(DISTINCT v ORDER BY v) FROM (
            SELECT min_monthly_income AS v FROM products WHERE min_monthly_income IS NOT NULL
            UNION SELECT value FROM filter_rules WHERE field = 'monthly_income' AND op = '>='
            UNION SELECT value + 1 FROM filter_rules WHERE field = 'monthly_income' AND op = '<='
            UNION SELECT -1) s) AS income,
        (SELECT array_agg(DISTINCT v ORDER BY v) FROM (
            SELECT min_credit_score::numeric AS v FROM products WHERE min_credit_score IS NOT NULL
            UNION SELECT value FROM filter_rules WHERE field = 'credit_score' AND op = '>='
            UNION SELECT value + 1 FROM filter_rules WHERE field = 'credit_score' AND op = '<='
            UNION SELECT -1) s) AS score,
        (SELECT array_agg(DISTINCT v ORDER BY v) FROM (
            SELECT min_age::numeric AS v FROM products WHERE min_age IS NOT NULL
            UNION SELECT max_age + 1 FROM products WHERE max_age IS NOT NULL
            UNION SELECT value FROM filter_rules WHERE field = 'age' AND op = '>='
            UNION SELECT value + 1 FROM filter_rules WHERE field = 'age' AND op = '<='
            UNION SELECT -1) s) AS age
),
batch AS (
    SELECT u.user_id, u.monthly_income, u.credit_score, u.age, u.employment_status,
           width_bucket(u.monthly_income, c.income) AS bi,
           width_bucket(u.credit_score::numeric, c.score) AS bs,
           width_bucket(u.age::numeric, c.age) AS ba
    FROM users u CROSS JOIN cuts c
    WHERE u.batch_id = $1
),
-- One representative per occupied cell: any member gives every product the same answer.
cells AS (
    SELECT bi, bs, ba, employment_status,
           min(monthly_income) AS monthly_income, min(credit_score) AS credit_score, min(age) AS age
    FROM batch
    GROUP BY bi, bs, ba, employment_status
),
cell_products AS (
    SELECT c.bi, c.bs, c.ba, c.employment_status, p.product_id, p.criteria_hash, p.has_fuzzy_criteria
    FROM cells c
    JOIN products p
      ON (p.min_monthly_income IS NULL OR c.monthly_income >= p.min_monthly_income)
     AND (p.min_credit_score IS NULL OR c.credit_score >= p.min_credit_score)
     AND (p.min_age IS NULL OR c.age >= p.min_age)
     AND (p.max_age IS NULL OR c.age <= p.max_age)
     AND (p.allowed_employment IS NULL OR c.employment_status = ANY (p.allowed_employment))
    WHERE NOT EXISTS (
        SELECT 1
        FROM filter_rules fr
        WHERE fr.product_id = p.product_id
          AND (cardinality(fr.when_employment) = 0 OR c.employment_status = ANY (fr.when_employment))
          AND NOT CASE fr.op
                      WHEN '>=' THEN CASE fr.field
                                         WHEN 'monthly_income' THEN c.monthly_income >= fr.value
                                         WHEN 'credit_score' THEN c.credit_score >= fr.value
                                         WHEN 'age' THEN c.age >= fr.value
                                     END
                      WHEN '<=' THEN CASE fr.field
                                         WHEN 'monthly_income' THEN c.monthly_income <= fr.value
                                         WHEN 'credit_score' THEN c.credit_score <= fr.value
                                         WHEN 'age' THEN c.age <= fr.value
                                     END
                  END
    )
)
SELECT b.user_id, cp.product_id, cp.criteria_hash,
       CASE WHEN cp.has_fuzzy_criteria THEN 'pending_llm' ELSE 'matched' END AS status
FROM batch b
JOIN cell_products cp
  USING (bi, bs, ba, employment_status)
