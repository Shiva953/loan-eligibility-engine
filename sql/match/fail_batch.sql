-- Workflow B failure path: record why the batch failed, so the UI and the sweeper can see it.
-- Always returns one row so the batch loop moves on. Parameters: #1 batch_id, #2 error.
WITH failed AS (
    UPDATE ingestion_batches
    SET status = 'failed', failed_at = now(), error = left($2, 2000)
    WHERE batch_id = $1::uuid
    RETURNING batch_id
)
SELECT $1::text AS batch_id, EXISTS (SELECT 1 FROM failed) AS marked_failed;
