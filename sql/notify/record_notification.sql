-- Workflow C, once per email: record what SES did with one pending email.
-- 'sent' also clears the user's to_announce and stamps notified_at, so the user is announced
-- again only when a later write changes their offers. Only a 'pending' row changes, so a repeated call is a no-op.
-- The batch's last pending email also closes the batch ('notified') and writes the notify
-- metric, so Workflow C needs no separate finishing statement (docs/DECISIONS.md AD27). Emails
-- are recorded one at a time by one run, so "last" is well defined.
-- Parameters: #1 notification_id, #2 outcome ('sent' | 'failed'), #3 SES message id,
--             #4 error message, #5 execution id
WITH updated AS (
    UPDATE notifications
    SET status = $2,
        ses_message_id = nullif($3, ''),
        error = nullif(left($4, 2000), ''),
        sent_at = CASE WHEN $2 = 'sent' THEN now() END
    WHERE notification_id = $1::bigint AND status = 'pending'
    RETURNING batch_id, user_id, status
),
marked AS (
    UPDATE user_matches m
    SET to_announce = false, notified_at = now()
    FROM updated u
    WHERE u.status = 'sent'
      AND m.batch_id = u.batch_id AND m.user_id = u.user_id AND m.to_announce
    RETURNING (SELECT count(*) FROM unnest(m.statuses) AS s WHERE s IN ('matched', 'llm_matched')) AS offers
),
-- The batch's notifications with this one at its new status (the snapshot still shows it as
-- pending, so it is counted from updated instead).
tally AS (
    SELECT u.batch_id,
           count(*) FILTER (WHERE n.status = 'sent') AS sent,
           count(*) FILTER (WHERE n.status = 'suppressed') AS suppressed,
           count(*) FILTER (WHERE n.status = 'failed') AS failed,
           count(*) FILTER (WHERE n.status = 'pending') AS pending
    FROM updated u
    JOIN LATERAL (
        SELECT status FROM notifications
        WHERE batch_id = u.batch_id AND notification_id <> $1::bigint
        UNION ALL
        SELECT u.status
    ) n ON true
    GROUP BY u.batch_id
),
closed AS (
    UPDATE ingestion_batches b
    SET status = 'notified', notified_at = now()
    FROM tally t
    WHERE b.batch_id = t.batch_id AND b.status = 'notifying' AND t.pending = 0
    RETURNING b.batch_id
),
metrics AS (
    INSERT INTO pipeline_metrics (run_id, batch_id, workflow, stage, pairs_in, pairs_out, details)
    SELECT $5, t.batch_id, 'C', 'notify', t.sent + t.suppressed + t.failed + t.pending, t.sent,
           jsonb_build_object('sent', t.sent, 'suppressed', t.suppressed,
                              'failed', t.failed, 'pending', t.pending)
    FROM tally t
    JOIN closed c USING (batch_id)
    RETURNING 1
)
SELECT $1::bigint AS notification_id,
       (SELECT status FROM updated) AS status,
       (SELECT coalesce(sum(offers), 0) FROM marked) AS matches_marked,
       EXISTS (SELECT 1 FROM closed) AS batch_finished,
       (SELECT count(*) FROM metrics) AS metrics_written;
