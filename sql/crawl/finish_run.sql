-- Workflow A: close a crawl run and update the source's bookkeeping in one statement.
--
-- Parameters: #1 run_id, #2 status, #3 http_status, #4 content_hash, #5 products_found,
--             #6 llm_calls, #7 tokens_in, #8 tokens_out, #9 error
--
-- On success ('changed' or 'unchanged') the source remembers the content hash, so the next run
-- can skip the LLM when the page hasn't changed. On 'unchanged' the source's products are
-- also marked as seen: they are still on the page, and without this they would be deactivated
-- after PRODUCT_STALE_DAYS of an unchanged page. Always returns exactly one row, so the
-- workflow's loop never stalls on an empty result.
WITH run AS (
    UPDATE crawl_runs
    SET status         = $2,
        http_status    = $3::integer,
        content_hash   = $4,
        products_found = $5::integer,
        llm_calls      = COALESCE($6::integer, 0),
        tokens_in      = COALESCE($7::integer, 0),
        tokens_out     = COALESCE($8::integer, 0),
        error          = $9,
        finished_at    = now()
    WHERE run_id = $1::bigint
    RETURNING run_id, source_id, status, content_hash
),
source AS (
    UPDATE crawl_sources s
    SET last_content_hash = run.content_hash,
        last_crawled_at   = now()
    FROM run
    WHERE s.source_id = run.source_id
      AND run.status IN ('changed', 'unchanged')
    RETURNING s.source_id
),
seen AS (
    UPDATE loan_products p
    SET last_seen_at = now()
    FROM run
    WHERE p.source_id = run.source_id
      AND p.is_active
      AND run.status = 'unchanged'
    RETURNING p.product_id
)
SELECT run.run_id,
       run.status,
       (SELECT count(*) FROM seen) AS products_touched
FROM run;
