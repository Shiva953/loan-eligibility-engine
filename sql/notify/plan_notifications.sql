-- Workflow C, first statement: decide who gets an email for this batch, record everyone, and build the
-- content of the few emails that will be rendered. One statement, so the decision is atomic.
--
-- * Candidates: users of the batch whose user_matches row has offers to announce (to_announce:
--   matched or llm_matched pairs no email announced yet) and no notifications row for this
--   batch yet. A rerun finds
--   every user already recorded and does nothing. Suppressed rows keep an empty product_ids:
--   nothing was announced to them, and their matches are in the matches table.
-- * In test_aliases mode the first EMAIL_CAP candidates (by user id, so a rerun of a fresh
--   batch picks the same people) get status 'pending' and a recipient taken from
--   EMAIL_ALIASES, in order. Everyone else is recorded 'suppressed' in the same INSERT. In
--   suppressed mode everyone is 'suppressed'.
-- * Email content is built only for those first EMAIL_CAP users: in suppressed mode as a
--   preview that is rendered and never sent. users.email is never selected: the address from
--   the CSV cannot reach the workflow.
-- * The same product is often listed twice, by an aggregator and by the lender itself
--   ("HDFC Bank Personal Loan" on BankBazaar, "HDFC Personal Loan" on hdfc.bank.in). An email
--   shows each (lender, product) once, preferring the lender's own page, then a stated rate,
--   then the lowest rate. Products are compared by their identity_key (lender + name without
--   punctuation, "Bank", "Ltd" or the lender's own name; migration 010).
-- * One round trip (docs/DECISIONS.md AD27). The statement reads app_config itself and checks
--   the email switches with the same rules as the "Check email settings" Code node (AD5)
--   before anything is recorded: with a hand-edited app_config it claims and records nothing
--   and returns config_error, and the Code node then stops the workflow with the reason.
-- * It also closes the batch when there is nothing to send (suppressed mode, or a rerun):
--   'notified' straight away, with the notify metric. Otherwise the batch is 'notifying', and
--   the last recorded email closes it (record_notification.sql).
--
-- * JIT is off: the planner estimates this statement at about 1.5M, so JIT compiled it for
--   0.7 s every run, while it executes in about 0.25 s at 10k users (1.25 s -> 0.27 s).
--
-- Parameters: #1 batch_id ('' = the most recently matched batch), #2 execution id
SET LOCAL jit = off;

WITH config AS (
    SELECT jsonb_object_agg(key, value) AS config FROM app_config
),
settings AS (
    SELECT c.config,
           coalesce(c.config ->> 'email_mode', '') AS mode,
           a.aliases,
           CASE WHEN c.config ->> 'email_cap' ~ '^[0-9]+$' AND length(c.config ->> 'email_cap') <= 4
                THEN (c.config ->> 'email_cap')::integer END AS cap,
           coalesce(c.config ->> 'ses_sender', '') AS sender,
           coalesce(c.config ->> 'ses_max_per_second', '') AS per_second
    FROM config c,
         LATERAL (SELECT coalesce(array_agg(btrim(x) ORDER BY o) FILTER (WHERE btrim(x) <> ''),
                                  '{}') AS aliases
                  FROM unnest(string_to_array(coalesce(c.config ->> 'email_aliases', ''), ','))
                       WITH ORDINALITY AS u (x, o)) a
),
-- CASE, not AND, guards each cast: Postgres does not promise to evaluate AND left to right.
checked AS MATERIALIZED (
    SELECT s.*,
           CASE
               WHEN s.mode NOT IN ('suppressed', 'test_aliases')
                   THEN 'EMAIL_MODE must be "suppressed" or "test_aliases"'
               WHEN cardinality(s.aliases) = 0
                    OR EXISTS (SELECT 1 FROM unnest(s.aliases) x
                               WHERE x !~ '^[^@\s,]+@[^@\s,]+\.[^@\s,]+$')
                   THEN 'EMAIL_ALIASES must be a comma-separated list of email addresses'
               WHEN (SELECT count(DISTINCT lower(x)) FROM unnest(s.aliases) x)
                    <> cardinality(s.aliases)
                   THEN 'EMAIL_ALIASES lists the same address twice'
               WHEN s.cap IS NULL OR s.cap < 1 OR s.cap > cardinality(s.aliases)
                   THEN 'EMAIL_CAP must be between 1 and ' || cardinality(s.aliases)
                        || ' (the number of aliases)'
               WHEN s.sender !~ '^[^@\s,]+@[^@\s,]+\.[^@\s,]+$'
                   THEN 'SES_SENDER is not an email address'
               WHEN NOT CASE WHEN s.per_second ~ '^[0-9]+(\.[0-9]+)?$'
                             THEN s.per_second::numeric > 0 ELSE false END
                   THEN 'SES_MAX_PER_SECOND must be a positive number'
           END AS config_error
    FROM settings s
),
target AS (
    SELECT coalesce(
               nullif($1, '')::uuid,
               (SELECT batch_id FROM ingestion_batches
                WHERE status IN ('matched', 'notifying')
                ORDER BY matched_at DESC NULLS LAST LIMIT 1)
           ) AS batch_id
),
-- The batch's own user_matches rows, found through the BRIN index: a batch's rows were written
-- together, so they sit side by side, and one row per user says whether there are offers to
-- announce. The batch id is read as a scalar subquery (a plan parameter), so the anti-join can
-- use the (batch_id, user_id) index instead of reading every notification ever recorded.
candidates AS MATERIALIZED (
    SELECT m.user_id
    FROM user_matches m
    WHERE m.batch_id = (SELECT batch_id FROM target)
      AND m.to_announce
      AND (SELECT config_error IS NULL FROM checked)
      AND NOT EXISTS (
          SELECT 1 FROM notifications n
          WHERE n.batch_id = (SELECT batch_id FROM target) AND n.user_id = m.user_id
      )
),
planned AS MATERIALIZED (
    SELECT r.user_id, r.rn,
           CASE WHEN s.mode = 'test_aliases' AND r.rn <= s.cap THEN 'pending' ELSE 'suppressed' END
               AS status,
           CASE WHEN s.mode = 'test_aliases' AND r.rn <= s.cap THEN s.aliases[r.rn::integer] END
               AS recipient
    FROM (SELECT c.*, row_number() OVER (ORDER BY c.user_id) AS rn FROM candidates c) r,
         checked s
),
-- product_ids lists what an email announces, so it is filled only for emails about to be sent.
recorded AS (
    INSERT INTO notifications (batch_id, user_id, status, email_mode, recipient, product_ids)
    SELECT t.batch_id, p.user_id, p.status, (SELECT mode FROM checked), p.recipient,
           CASE WHEN p.status = 'pending' THEN ARRAY(
               SELECT x.product_id FROM user_matches m, unnest(m.product_ids, m.statuses) AS x (product_id, status)
               WHERE m.batch_id = t.batch_id AND m.user_id = p.user_id
                 AND x.status IN ('matched', 'llm_matched')
               ORDER BY x.product_id)
           ELSE '{}' END
    FROM planned p, target t
    WHERE t.batch_id IS NOT NULL
    ORDER BY p.user_id
    ON CONFLICT (batch_id, user_id) DO NOTHING
    RETURNING notification_id, user_id, status, recipient
),
to_render AS MATERIALIZED (
    SELECT r.notification_id, r.user_id, r.status, r.recipient, p.rn, u.name
    FROM recorded r
    JOIN planned p USING (user_id)
    JOIN users u USING (user_id)
    WHERE p.rn <= (SELECT cap FROM checked)
),
offers AS (
    SELECT tr.notification_id, x.status AS match_status,
           coalesce(ARRAY(SELECT jsonb_array_elements_text(m.details -> x.product_id::text -> 'caveats')), '{}') AS caveats,
           p.lender, p.product_name, p.interest_rate_min, p.interest_rate_max,
           ARRAY(SELECT c ->> 'text' FROM jsonb_array_elements(p.soft_criteria) c
                 WHERE c ->> 'class' = 'unknown_field') AS conditions,
           row_number() OVER (
               PARTITION BY tr.notification_id, p.identity_key
               ORDER BY (s.kind = 'lender') DESC, (p.interest_rate_min IS NULL),
                        p.interest_rate_min, length(p.product_name) DESC, p.product_id
           ) AS pick
    FROM to_render tr
    JOIN user_matches m
      ON m.batch_id = (SELECT batch_id FROM target) AND m.user_id = tr.user_id
    CROSS JOIN LATERAL unnest(m.product_ids, m.statuses) AS x (product_id, status)
    JOIN loan_products p ON p.product_id = x.product_id
    JOIN crawl_sources s ON s.source_id = p.source_id
    WHERE x.status IN ('matched', 'llm_matched')
),
emails AS (
    SELECT tr.notification_id, tr.user_id, tr.status, tr.recipient, tr.rn, tr.name,
           coalesce((
               SELECT jsonb_agg(jsonb_build_object(
                          'lender', o.lender, 'product', o.product_name,
                          'rate_min', o.interest_rate_min, 'rate_max', o.interest_rate_max,
                          'status', o.match_status, 'caveats', to_jsonb(o.caveats),
                          'conditions', to_jsonb(o.conditions))
                      ORDER BY (o.match_status = 'matched') DESC, o.interest_rate_min NULLS LAST,
                               o.lender, o.product_name)
               FROM offers o
               WHERE o.notification_id = tr.notification_id AND o.pick = 1
           ), '[]'::jsonb) AS products
    FROM to_render tr
),
counts AS (
    SELECT count(*) FILTER (WHERE status = 'pending') AS to_send,
           count(*) FILTER (WHERE status = 'suppressed') AS suppressed,
           round(extract(epoch FROM clock_timestamp() - statement_timestamp()) * 1000)::integer AS ms
    FROM recorded
),
-- Nothing pending means nothing to send: the batch is closed here. A batch left 'notifying' by
-- an earlier run is closed by a rerun too; its leftover pending rows are never sent (SES may
-- already have sent them) and show up in the notify metric.
claimed AS (
    UPDATE ingestion_batches b
    SET status = CASE WHEN c.to_send > 0 THEN 'notifying' ELSE 'notified' END,
        notified_at = CASE WHEN c.to_send > 0 THEN b.notified_at ELSE now() END
    FROM target t, counts c, checked s
    WHERE s.config_error IS NULL
      AND b.batch_id = t.batch_id AND b.status IN ('matched', 'notifying')
    RETURNING b.status
),
-- Every notification of the batch: the snapshot (earlier runs) plus the rows written above.
tally AS (
    SELECT count(*) FILTER (WHERE status = 'sent') AS sent,
           count(*) FILTER (WHERE status = 'suppressed') AS suppressed,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'pending') AS pending
    FROM (SELECT status FROM notifications WHERE batch_id = (SELECT batch_id FROM target)
          UNION ALL
          SELECT status FROM recorded) n
),
metrics AS (
    INSERT INTO pipeline_metrics (run_id, batch_id, workflow, stage, pairs_in, pairs_out, duration_ms, details)
    SELECT $2, t.batch_id, 'C', v.stage, v.pairs_in, v.pairs_out, v.ms, v.details
    FROM target t, counts c, tally y, checked s,
         LATERAL (VALUES
             ('plan_notifications', (SELECT count(*) FROM candidates), c.to_send, c.ms,
              jsonb_build_object('email_mode', s.mode, 'email_cap', s.cap,
                                 'suppressed', c.suppressed),
              true),
             ('notify', y.sent + y.suppressed + y.failed + y.pending, y.sent, NULL::integer,
              jsonb_build_object('sent', y.sent, 'suppressed', y.suppressed,
                                 'failed', y.failed, 'pending', y.pending),
              EXISTS (SELECT 1 FROM claimed WHERE status = 'notified'))
         ) AS v (stage, pairs_in, pairs_out, ms, details, wanted)
    WHERE t.batch_id IS NOT NULL AND s.config_error IS NULL AND v.wanted
    RETURNING 1
)
SELECT t.batch_id::text AS batch_id,
       s.config_error,
       s.config,
       s.mode AS email_mode,
       (SELECT count(*) FROM candidates) AS users_with_matches,
       c.to_send,
       c.suppressed,
       (SELECT status FROM claimed) AS batch_status,
       (SELECT count(*) FROM metrics) AS metrics_written,
       coalesce((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.rn) FROM emails e), '[]'::jsonb) AS emails
FROM target t, counts c, checked s;
