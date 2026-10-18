-- Workflow A, after every source: products not seen for #1 days have left their page, so they
-- stop being matched. Returns a single count row so the node always emits one item.
WITH stale AS (
    UPDATE loan_products
    SET is_active = false
    WHERE is_active
      AND last_seen_at < now() - make_interval(days => $1::integer)
    RETURNING product_id
)
SELECT count(*) AS products_deactivated FROM stale;
