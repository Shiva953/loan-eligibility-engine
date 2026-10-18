-- Workflow B follow-ups on matches: a BRIN batch index, and a fix to the delete guard.
--
-- 1. The per-batch lookup index becomes BRIN. Every match row of a batch is written by one
--    INSERT, so a batch's rows sit in consecutive heap pages and a block-range summary of
--    batch_id locates them almost exactly. The B-tree on (batch_id, status) cost one extra
--    index entry, about 80 bytes of WAL, for every row written: at 100k users (2.37M rows) it
--    took WAL from 766 MB to 960 MB, and the write ran 39 s against 24 s for BRIN (medians of
--    two runs). The per-batch count query was also faster on BRIN (0.35-0.58 s against
--    0.92-1.37 s), because it reads the batch's pages in order instead of visiting the heap
--    once per index entry. docs/EXPERIMENTS.md, write path.
--    autosummarize asks autovacuum to summarise each range once it fills, so ranges written by
--    older batches don't stay unsummarised (and scanned on every lookup) until the next VACUUM.
--
-- 2. matches_restrict_parent() (005) tested both tables in one IF expression. PL/pgSQL plans an
--    expression whole, and old_rows only has the deleted table's columns, so deleting any
--    product failed on batch_id (and any batch on product_id), referenced or not. Each table
--    now has its own branch, so only the matching query is ever planned.
DROP INDEX matches_batch_status_idx;

CREATE INDEX matches_batch_brin ON matches USING brin (batch_id)
    WITH (pages_per_range = 32, autosummarize = on);

CREATE OR REPLACE FUNCTION matches_restrict_parent() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'loan_products' THEN
        IF EXISTS (SELECT 1 FROM matches m JOIN old_rows o USING (product_id)) THEN
            RAISE EXCEPTION 'loan_products: rows are still referenced by matches'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    ELSIF EXISTS (SELECT 1 FROM matches m JOIN old_rows o USING (batch_id)) THEN
        RAISE EXCEPTION 'ingestion_batches: rows are still referenced by matches'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END
$$;
