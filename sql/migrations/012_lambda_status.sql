-- The Lambdas' role (lambda_app) for the upload page's status endpoint and for ingest:
--   * status reads the stage metrics and the email outcome of a batch (no user data in either);
--   * ingest ends with ANALYZE users so Workflow B plans the new batch with fresh statistics.
--     In Postgres 17 that needs MAINTAIN; without it ANALYZE skips the table with only a warning.
GRANT SELECT ON pipeline_metrics, notifications TO lambda_app;
GRANT MAINTAIN ON users TO lambda_app;
