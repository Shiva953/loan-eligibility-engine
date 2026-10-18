-- Workflow C records an email before sending it (status 'pending'), then marks it 'sent' or
-- 'failed'. The (batch, user) row exists before the SES call, so neither a rerun nor a retry
-- after a crash can email the same user about the same batch twice: they skip users that
-- already have a row. The price is at-most-once delivery: an email interrupted between the
-- record and the send stays 'pending' and is not retried automatically.
ALTER TABLE notifications DROP CONSTRAINT notifications_status_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_status_check
    CHECK (status IN ('pending', 'sent', 'suppressed', 'failed'));

-- When SES accepted the email (status 'sent').
ALTER TABLE notifications ADD COLUMN sent_at timestamptz;
