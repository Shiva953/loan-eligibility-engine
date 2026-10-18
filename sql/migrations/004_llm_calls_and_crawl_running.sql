-- Phase 2a.
--
-- 1. llm_calls: one row per provider request made by the LLM gateway sub-workflow, including
--    rate-limited attempts. It backs the daily request budget (free-tier RPD) and gives the
--    bench exact call and token counts instead of estimates.
-- 2. crawl_runs gains a 'running' state. Workflow A inserts the row when it starts on a source
--    and finishes it at the end, so a run that dies midway stays visible as 'running'
--    instead of leaving no trace.

ALTER TABLE crawl_runs DROP CONSTRAINT crawl_runs_status_check;
ALTER TABLE crawl_runs ADD CONSTRAINT crawl_runs_status_check
    CHECK (status IN ('running', 'changed', 'unchanged', 'robots_blocked', 'error'));
ALTER TABLE crawl_runs ALTER COLUMN status SET DEFAULT 'running';

CREATE TABLE llm_calls (
    call_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_id  text,
    purpose       text NOT NULL,
    provider      text NOT NULL,
    model         text NOT NULL,
    attempt       smallint NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    http_status   integer,
    outcome       text NOT NULL CHECK (outcome IN ('ok', 'rate_limited', 'error')),
    tokens_in     integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out    integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    duration_ms   integer CHECK (duration_ms >= 0),
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_created_idx ON llm_calls (created_at);

GRANT SELECT, INSERT ON llm_calls TO n8n_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO n8n_app;
