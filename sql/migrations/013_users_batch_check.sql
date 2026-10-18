-- users.batch_id: one check per statement instead of a row-level foreign key.
--
-- Why: a foreign key's trigger runs once per row, and ingest upserts every row of a file under
-- one batch_id. At 100k rows the check took 354 ms of a 2.3 s load (EXPLAIN ANALYZE), finding
-- the same batch 100k times. One check over the statement's distinct batch ids does the same
-- job; user_matches already works this way (migration 011, AD31). The check takes FOR KEY SHARE
-- on the batch, as the foreign key did, so a concurrent delete of the batch either waits for
-- the upload to commit and is then refused, or wins and the upload fails. Deleting a batch that
-- users still reference is refused as before, and a batch id can't change.
--
-- The updated_at trigger now runs only when the statement didn't set updated_at itself: ingest
-- sets it in the upsert, so the trigger's per-row call (57 ms at 100k rows) did nothing.

ALTER TABLE users DROP CONSTRAINT users_batch_id_fkey;

CREATE FUNCTION users_check_batch() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    wanted bigint;
    found bigint;
BEGIN
    SELECT count(DISTINCT n.batch_id) INTO wanted FROM new_rows n;
    SELECT count(*) INTO found
    FROM (SELECT 1 FROM ingestion_batches b
          WHERE b.batch_id IN (SELECT n.batch_id FROM new_rows n)
          FOR KEY SHARE OF b) locked;
    IF found < wanted THEN
        RAISE EXCEPTION 'users: reference to a missing ingestion batch'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER users_batch_insert AFTER INSERT ON users
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION users_check_batch();
CREATE TRIGGER users_batch_update AFTER UPDATE ON users
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION users_check_batch();

CREATE FUNCTION users_restrict_batches() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM users u JOIN old_rows o USING (batch_id)) THEN
        RAISE EXCEPTION 'ingestion_batches: rows are still referenced by users'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER ingestion_batches_restrict_users AFTER DELETE ON ingestion_batches
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION users_restrict_batches();

CREATE FUNCTION forbid_key_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%: the key of a row can''t change', TG_TABLE_NAME
        USING ERRCODE = 'foreign_key_violation';
END
$$;
CREATE TRIGGER ingestion_batches_key_fixed BEFORE UPDATE OF batch_id ON ingestion_batches
    FOR EACH ROW WHEN (OLD.batch_id IS DISTINCT FROM NEW.batch_id)
    EXECUTE FUNCTION forbid_key_change();

DROP TRIGGER users_updated_at ON users;
CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW WHEN (OLD.updated_at IS NOT DISTINCT FROM NEW.updated_at)
    EXECUTE FUNCTION set_updated_at();
