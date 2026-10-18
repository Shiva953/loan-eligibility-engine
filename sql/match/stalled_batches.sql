-- Workflow B: batches waiting to be matched, for the sweeper and for "Run manually".
-- The sweeper takes batches that should have been matched by now: still 'ingested' after
-- sweeper_stale_minutes (the webhook never arrived), or stuck in 'matching' for 30 minutes (a
-- run died midway). "Run manually" passes 0 minutes, so a click in the editor matches every
-- batch that is waiting, at once and on the canvas. Oldest first, at most 10 per run.
-- Parameters: #1 minimum age in minutes ('' = app_config sweeper_stale_minutes),
--             #2 who is asking ('sweeper' or 'manual')
SELECT batch_id::text AS batch_id, $2 AS trigger
FROM ingestion_batches
WHERE (status = 'ingested'
       AND ingested_at <= now() - make_interval(mins => coalesce(
           nullif($1, '')::integer,
           (SELECT value::integer FROM app_config WHERE key = 'sweeper_stale_minutes'))))
   OR (status = 'matching' AND matching_at < now() - interval '30 minutes')
ORDER BY received_at
LIMIT 10;
