-- Matches are stored as one row per user instead of one row per (user, product) pair
-- (DECISIONS AD31).
--
-- A batch of 10k users wrote about 214k pair rows (2.1M at 100k): each a heap tuple, an entry
-- in a primary key on a random UUID, and the WAL for both, and that write was the largest part
-- of every batch. The same decisions now take one row per user:
--   * product_ids / statuses: the user's pairs as parallel arrays sorted by product id;
--   * details: the few pairs with a score, a reason or caveats (about 5% on the real products),
--     keyed by product id; decided_by follows from the status;
--   * to_announce: the user has offers (matched or llm_matched) that no email has announced yet,
--     which replaces the per-pair notified_at that Workflow C read.
-- Measured on the same batches, every path ran faster or equally fast (EXPERIMENTS section 12).
--
-- `matches` stays, as a read-only view with the old per-pair columns, so everything that reads
-- pairs (the bench's recall check, the end-to-end check, the docs' queries) is unchanged.

CREATE TABLE user_matches (
    user_id      uuid PRIMARY KEY,
    batch_id     uuid NOT NULL,
    product_ids  bigint[] NOT NULL,
    statuses     text[] NOT NULL,
    details      jsonb NOT NULL DEFAULT '{}',
    to_announce  boolean NOT NULL DEFAULT false,
    notified_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_matches_arrays_ck CHECK (
        cardinality(product_ids) = cardinality(statuses)
        AND statuses <@ ARRAY['matched', 'llm_matched', 'llm_rejected', 'pending_llm']::text[]),
    CONSTRAINT user_matches_details_ck CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX user_matches_batch_brin ON user_matches USING brin (batch_id)
    WITH (pages_per_range = 32, autosummarize = on);

-- References, checked once per statement as before (AD19): users and batches by key, products
-- by the distinct ids the statement wrote.
CREATE FUNCTION user_matches_check_refs() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM new_rows n
               WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = n.user_id))
    OR EXISTS (SELECT 1 FROM (SELECT DISTINCT unnest(n.product_ids) AS product_id FROM new_rows n) x
               WHERE NOT EXISTS (SELECT 1 FROM loan_products p WHERE p.product_id = x.product_id))
    OR EXISTS (SELECT 1 FROM (SELECT DISTINCT n.batch_id FROM new_rows n) n
               WHERE NOT EXISTS (SELECT 1 FROM ingestion_batches b WHERE b.batch_id = n.batch_id))
    THEN
        RAISE EXCEPTION 'user_matches: reference to a missing user, product or batch'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER user_matches_refs_insert AFTER INSERT ON user_matches
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION user_matches_check_refs();
CREATE TRIGGER user_matches_refs_update AFTER UPDATE ON user_matches
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION user_matches_check_refs();

-- Existing pairs move over: each user's pairs from the user's current batch, the only ones any
-- workflow reads again (Workflow C works on one batch, and a re-upload replaced the rest).
INSERT INTO user_matches (user_id, batch_id, product_ids, statuses, details, to_announce,
                          notified_at, created_at, updated_at)
SELECT m.user_id, m.batch_id,
       array_agg(m.product_id ORDER BY m.product_id),
       array_agg(m.status ORDER BY m.product_id),
       coalesce(jsonb_object_agg(m.product_id::text, jsonb_strip_nulls(jsonb_build_object(
                    'score', m.score, 'reason', m.reason,
                    'caveats', CASE WHEN cardinality(m.caveats) > 0 THEN to_jsonb(m.caveats) END)))
                FILTER (WHERE m.score IS NOT NULL OR m.reason IS NOT NULL
                              OR cardinality(m.caveats) > 0),
                '{}'::jsonb),
       bool_or(m.status IN ('matched', 'llm_matched') AND m.notified_at IS NULL),
       max(m.notified_at), min(m.created_at), max(m.updated_at)
FROM matches m
JOIN users u ON u.user_id = m.user_id AND u.batch_id = m.batch_id
GROUP BY m.user_id, m.batch_id;

-- The parent-table guards now look at user_matches (separate IF branches: V27).
CREATE OR REPLACE FUNCTION matches_cascade_users() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM user_matches m USING old_rows o WHERE m.user_id = o.user_id;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matches_restrict_parent() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'loan_products' THEN
        IF EXISTS (SELECT 1 FROM user_matches m
                   WHERE m.product_ids && ARRAY(SELECT o.product_id FROM old_rows o)) THEN
            RAISE EXCEPTION 'loan_products: rows are still referenced by matches'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    ELSIF EXISTS (SELECT 1 FROM user_matches m JOIN old_rows o USING (batch_id)) THEN
        RAISE EXCEPTION 'ingestion_batches: rows are still referenced by matches'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;

DROP TABLE matches;
DROP FUNCTION matches_check_refs();

-- The per-pair view. notified_at is the user's last email, and to_announce whether the user
-- still has offers to announce.
CREATE VIEW matches AS
SELECT um.user_id,
       x.product_id,
       um.batch_id,
       x.status,
       CASE x.status WHEN 'matched' THEN 'rule' WHEN 'pending_llm' THEN NULL ELSE 'llm' END
           AS decided_by,
       (um.details -> x.product_id::text ->> 'score')::numeric(6, 3) AS score,
       um.details -> x.product_id::text ->> 'reason' AS reason,
       coalesce(ARRAY(SELECT jsonb_array_elements_text(um.details -> x.product_id::text -> 'caveats')),
                '{}'::text[]) AS caveats,
       um.to_announce,
       um.notified_at,
       um.created_at,
       um.updated_at
FROM user_matches um
CROSS JOIN LATERAL unnest(um.product_ids, um.statuses) AS x (product_id, status);

GRANT SELECT, INSERT, UPDATE ON user_matches TO n8n_app;
GRANT SELECT ON matches TO n8n_app;
