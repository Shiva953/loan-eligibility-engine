-- Initial data model. It lands in one migration because the tables reference each other;
-- every later change gets its own numbered file.
--
-- Conventions:
--   * A NULL eligibility threshold means "not stated by the lender" and must never filter a
--     user out. Recall comes first (docs/PLAN.md section 5, S2).
--   * Status columns are CHECK-constrained text rather than enums, so a migration can add a
--     state without ALTER TYPE.

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- One row per uploaded CSV. `status` is the pipeline's state machine, polled by the UI through
-- the status Lambda. Before ingest creates this row, the status Lambda reports "uploaded" from
-- the S3 object alone.
CREATE TABLE ingestion_batches (
    batch_id       uuid PRIMARY KEY,
    s3_bucket      text NOT NULL,
    s3_key         text NOT NULL,
    etag           text NOT NULL,
    status         text NOT NULL DEFAULT 'ingesting'
                   CHECK (status IN ('ingesting', 'ingested', 'matching', 'matched',
                                     'notifying', 'notified', 'failed')),
    rows_total     integer CHECK (rows_total >= 0),
    rows_valid     integer CHECK (rows_valid >= 0),
    rows_rejected  integer CHECK (rows_rejected >= 0),
    rejects_key    text,
    error          text,
    received_at    timestamptz NOT NULL DEFAULT now(),
    ingested_at    timestamptz,
    matching_at    timestamptz,
    matched_at     timestamptz,
    notified_at    timestamptz,
    failed_at      timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    -- S3 delivers events at least once; a redelivered event hits this and becomes a no-op.
    CONSTRAINT ingestion_batches_object_uq UNIQUE (s3_bucket, s3_key, etag)
);

CREATE TABLE users (
    user_id            uuid PRIMARY KEY,
    name               text NOT NULL,
    -- Not unique: the sample CSV reuses one address for up to 12 users (docs/PROFILE.md).
    email              text NOT NULL,
    monthly_income     numeric(12, 2) NOT NULL CHECK (monthly_income > 0),
    credit_score       smallint NOT NULL CHECK (credit_score BETWEEN 300 AND 900),
    employment_status  text NOT NULL
                       CHECK (employment_status IN ('Salaried', 'Self-Employed', 'Business')),
    age                smallint NOT NULL CHECK (age BETWEEN 18 AND 100),
    -- The batch that last inserted or updated this user; Workflow B matches by it.
    batch_id           uuid NOT NULL REFERENCES ingestion_batches (batch_id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX users_batch_id_idx ON users (batch_id);

-- Sites Workflow A crawls. One source can span several pages (BankBazaar keeps rates and
-- eligibility on separate pages); they are concatenated before hashing and extraction.
CREATE TABLE crawl_sources (
    source_id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug               text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
    display_name       text NOT NULL,
    kind               text NOT NULL CHECK (kind IN ('aggregator', 'lender')),
    page_urls          text[] NOT NULL CHECK (cardinality(page_urls) > 0),
    content_selector   text,
    fixture_files      text[],
    enabled            boolean NOT NULL DEFAULT true,
    last_content_hash  text,
    last_crawled_at    timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- One row per source per Workflow A execution, including skipped and failed ones.
CREATE TABLE crawl_runs (
    run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_id    text,
    source_id       integer NOT NULL REFERENCES crawl_sources (source_id),
    mode            text NOT NULL CHECK (mode IN ('live', 'fixtures')),
    status          text NOT NULL
                    CHECK (status IN ('changed', 'unchanged', 'robots_blocked', 'error')),
    http_status     integer,
    content_hash    text,
    products_found  integer CHECK (products_found >= 0),
    llm_calls       integer NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    tokens_in       integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out      integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    error           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX crawl_runs_source_started_idx ON crawl_runs (source_id, started_at DESC);

-- Products compiled at crawl time (S1). Hard thresholds are columns so S2 can filter in SQL;
-- everything else is classified in soft_criteria.
CREATE TABLE loan_products (
    product_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id           integer NOT NULL REFERENCES crawl_sources (source_id),
    source_url          text NOT NULL,
    lender              text NOT NULL,
    product_name        text NOT NULL,
    interest_rate_min   numeric(5, 2) CHECK (interest_rate_min BETWEEN 0 AND 100),
    interest_rate_max   numeric(5, 2) CHECK (interest_rate_max BETWEEN 0 AND 100),
    min_monthly_income  numeric(12, 2) CHECK (min_monthly_income >= 0),
    min_credit_score    smallint CHECK (min_credit_score BETWEEN 300 AND 900),
    min_age             smallint CHECK (min_age BETWEEN 18 AND 100),
    max_age             smallint CHECK (max_age BETWEEN 18 AND 100),
    -- NULL means any employment type. Values must use the users table's vocabulary, so
    -- extraction has to map lender wording before it can become a hard filter.
    allowed_employment  text[] CHECK (
        allowed_employment <@ ARRAY['Salaried', 'Self-Employed', 'Business']::text[]
    ),
    -- [{"text": ..., "class": "compilable"|"fuzzy"|"unknown_field", "depends_on": [...]}]
    soft_criteria       jsonb NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(soft_criteria) = 'array'),
    -- {"min_credit_score": 0.95, ...}: S6 distrusts any field below the threshold.
    field_confidence    jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(field_confidence) = 'object'),
    -- Derived, so it can never disagree with soft_criteria.
    has_fuzzy_criteria  boolean GENERATED ALWAYS AS (
        jsonb_path_exists(soft_criteria, '$[*] ? (@.class == "fuzzy")')
    ) STORED,
    needs_review        boolean NOT NULL DEFAULT false,
    criteria_json       jsonb NOT NULL,
    criteria_hash       text NOT NULL,
    content_hash        text NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT loan_products_source_product_uq UNIQUE (source_url, product_name),
    CONSTRAINT loan_products_rate_range_ck CHECK (interest_rate_min <= interest_rate_max),
    CONSTRAINT loan_products_age_range_ck CHECK (min_age <= max_age)
);
CREATE INDEX loan_products_active_idx ON loan_products (product_id) WHERE is_active;

-- Criteria changes between crawls, kept as before/after so a change can be shown as a diff.
CREATE TABLE product_criteria_history (
    history_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id      bigint NOT NULL REFERENCES loan_products (product_id) ON DELETE CASCADE,
    crawl_run_id    bigint REFERENCES crawl_runs (run_id),
    old_criteria    jsonb,
    new_criteria    jsonb NOT NULL,
    changed_fields  text[] NOT NULL,
    crawled_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX product_criteria_history_product_idx
    ON product_criteria_history (product_id, crawled_at DESC);

-- One row per eligible or pending user-product pair. Rejected pairs are not stored, except
-- those the LLM rejected (llm_rejected), which are kept as evidence.
CREATE TABLE matches (
    user_id        uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    product_id     bigint NOT NULL REFERENCES loan_products (product_id),
    batch_id       uuid NOT NULL REFERENCES ingestion_batches (batch_id),
    status         text NOT NULL
                   CHECK (status IN ('matched', 'pending_llm', 'llm_matched', 'llm_rejected')),
    decided_by     text CHECK (decided_by IN ('rule', 'llm')),
    -- The criteria version the decision was made against.
    criteria_hash  text NOT NULL,
    score          numeric(6, 3),
    reason         text,
    caveats        text[] NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    notified_at    timestamptz,
    PRIMARY KEY (user_id, product_id),
    CONSTRAINT matches_decision_ck CHECK (
        (status = 'matched' AND decided_by = 'rule')
        OR (status IN ('llm_matched', 'llm_rejected') AND decided_by = 'llm')
        OR (status = 'pending_llm' AND decided_by IS NULL)
    )
);
CREATE INDEX matches_batch_status_idx ON matches (batch_id, status);

-- S4 decision cache. A changed criterion gets a new hash, so stale verdicts stop applying on
-- their own.
CREATE TABLE llm_decisions (
    criteria_hash   text NOT NULL,
    user_signature  text NOT NULL,
    verdict         text NOT NULL CHECK (verdict IN ('eligible', 'ineligible', 'uncertain')),
    reason          text,
    model           text NOT NULL,
    prompt_version  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (criteria_hash, user_signature)
);

-- Per-stage counters written by the workflows and by the bench, so both report the same way.
CREATE TABLE pipeline_metrics (
    metric_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       text NOT NULL,
    batch_id     uuid REFERENCES ingestion_batches (batch_id),
    workflow     text NOT NULL,
    stage        text NOT NULL,
    pairs_in     bigint CHECK (pairs_in >= 0),
    pairs_out    bigint CHECK (pairs_out >= 0),
    llm_calls    integer NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    tokens_in    integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out   integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    duration_ms  integer CHECK (duration_ms >= 0),
    details      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pipeline_metrics_run_idx ON pipeline_metrics (run_id);
CREATE INDEX pipeline_metrics_batch_idx ON pipeline_metrics (batch_id);

-- Where each notification actually went (AD5). One row per user per batch, so a rerun of
-- Workflow C finds every user already handled and sends nothing.
CREATE TABLE notifications (
    notification_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id         uuid NOT NULL REFERENCES ingestion_batches (batch_id),
    user_id          uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    status           text NOT NULL CHECK (status IN ('sent', 'suppressed', 'failed')),
    email_mode       text NOT NULL CHECK (email_mode IN ('suppressed', 'test_aliases')),
    -- Always a verified test alias, never the address from the CSV.
    recipient        text,
    ses_message_id   text,
    product_ids      bigint[] NOT NULL DEFAULT '{}',
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT notifications_batch_user_uq UNIQUE (batch_id, user_id),
    CONSTRAINT notifications_sent_ck CHECK (
        status <> 'sent' OR (recipient IS NOT NULL AND ses_message_id IS NOT NULL)
    )
);

-- Non-secret switches copied from .env by `make config` (AD4). Workflows read this table in
-- their first node instead of $env, which n8n 2.x blocks by default.
CREATE TABLE app_config (
    key         text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]*$'),
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER ingestion_batches_updated_at BEFORE UPDATE ON ingestion_batches
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER crawl_sources_updated_at BEFORE UPDATE ON crawl_sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER loan_products_updated_at BEFORE UPDATE ON loan_products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER matches_updated_at BEFORE UPDATE ON matches
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER app_config_updated_at BEFORE UPDATE ON app_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
