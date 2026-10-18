-- Workflow A: open a crawl run for one source. It stays 'running' until finish_run.sql closes
-- it, so a run that dies midway is visible. Parameters: #1 execution id, #2 source id, #3 mode.
INSERT INTO crawl_runs (execution_id, source_id, mode)
VALUES ($1, $2::integer, $3)
RETURNING run_id, source_id, mode;
