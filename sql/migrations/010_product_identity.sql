-- A product is identified by its lender and name within a source, compared without
-- punctuation, without words like "Bank" or "Ltd", and without repeating the lender's name
-- (DECISIONS AD30, open issue O1). The LLM's wording varies between readings of the same page
-- ("HDFC Personal Loan", then "HDFC Bank Personal Loan"; "TurboLoan", then "TurboLoan powered
-- by Chola"). Keyed on the exact name, each re-extraction added a second product, every user was
-- matched to both, and an unchanged page kept both alive. Keyed on the identity, a re-extraction
-- updates the product it already has.
--
-- This is the comparison Workflow C has always used to show each product once in an email, now
-- in one function that the table, the crawl upsert and the email query share.

CREATE FUNCTION product_identity(p_lender text, p_product text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT l.lender_key || '|' || array_to_string(ARRAY(
               SELECT w
               FROM regexp_split_to_table(
                        lower(regexp_replace(p_product, '[^A-Za-z0-9]+', ' ', 'g')), ' ') AS w
               WHERE w <> ''
                 AND w <> ALL (string_to_array(l.lender_key, ' ')
                               || ARRAY['bank', 'ltd', 'limited', 'powered', 'by'])
           ), ' ')
    FROM (SELECT btrim(regexp_replace(
                     regexp_replace(lower(p_lender), '[^a-z0-9]+', ' ', 'g'),
                     '\s(bank|ltd|limited)\s*$', '')) AS lender_key) l
$$;

-- Copies left by earlier re-extractions of the same page: keep the newest reading of each
-- product (it reflects the current prompt and validation), active if any copy was. A copy that
-- match rows still reference can't be removed (the restrict trigger stops the migration):
-- re-run it after clearing those batches.
WITH ranked AS (
    SELECT product_id,
           row_number() OVER w AS rn,
           bool_or(is_active) OVER w AS any_active
    FROM loan_products
    WINDOW w AS (PARTITION BY source_id, product_identity(lender, product_name)
                 ORDER BY first_seen_at DESC, product_id DESC
                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
),
kept AS (
    UPDATE loan_products p
    SET is_active = r.any_active
    FROM ranked r
    WHERE p.product_id = r.product_id AND r.rn = 1 AND p.is_active IS DISTINCT FROM r.any_active
    RETURNING p.product_id
)
DELETE FROM loan_products p
USING ranked r
WHERE p.product_id = r.product_id AND r.rn > 1;

ALTER TABLE loan_products
    ADD COLUMN identity_key text NOT NULL
        GENERATED ALWAYS AS (product_identity(lender, product_name)) STORED,
    DROP CONSTRAINT loan_products_source_product_uq,
    ADD CONSTRAINT loan_products_identity_uq UNIQUE (source_id, identity_key);
