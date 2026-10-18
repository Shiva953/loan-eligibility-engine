-- Workflow A's final output: what this execution did, per source, plus totals.
-- Parameter: #1 execution id.
SELECT s.slug,
       r.status,
       r.http_status,
       r.products_found,
       r.llm_calls,
       r.tokens_in,
       r.tokens_out,
       r.error,
       (SELECT count(*) FROM loan_products p WHERE p.source_id = s.source_id AND p.is_active)
           AS active_products
FROM crawl_runs r
JOIN crawl_sources s USING (source_id)
WHERE r.execution_id = $1
ORDER BY s.source_id;
