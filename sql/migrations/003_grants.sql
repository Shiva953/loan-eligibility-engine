-- Least privilege per role (docs/DECISIONS.md AD1). There are no default privileges: a later
-- migration that adds a table must grant on it explicitly, which keeps every grant reviewable.

GRANT USAGE ON SCHEMA public TO n8n_app, lambda_app;

-- Lambdas: ingest writes batches and users; status reads them. Ingest's staging table is a
-- session TEMP table, which PUBLIC may create by default, so it needs no grant here.
GRANT SELECT, INSERT, UPDATE ON ingestion_batches, users TO lambda_app;

-- n8n: reads users and config, advances batch status, and owns crawl, match and notification
-- state. No DELETE anywhere: resets are an admin operation.
GRANT SELECT ON users, app_config TO n8n_app;
GRANT SELECT, UPDATE ON ingestion_batches TO n8n_app;
GRANT SELECT, INSERT, UPDATE
    ON crawl_sources, crawl_runs, loan_products, product_criteria_history,
       matches, llm_decisions, pipeline_metrics, notifications
    TO n8n_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO n8n_app;
