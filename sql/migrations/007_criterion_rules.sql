-- Workflow B, the second judge: fuzzy criteria compiled into rules (docs/EXPERIMENTS.md, LLM stage).
--
-- One row per fuzzy criterion, keyed like the cohort verdicts in llm_decisions (a hash of the
-- wording and depends_on), so products that share a criterion share its rule. clauses holds
-- the conditions the LLM wrote, each tagged eligible or ineligible:
--   [{"verdict": "ineligible", "employment_status": [], "credit_score_min": null,
--     "credit_score_max": 719, ...}]
-- A profile that no clause covers is uncertain, so a criterion the LLM can't express as
-- conditions (empty clauses) never decides anything on its own. write_matches.sql applies the
-- clauses to each user's exact facts and combines them with the cohort verdict.
CREATE TABLE criterion_rules (
    criterion_key   text PRIMARY KEY,
    criterion_text  text NOT NULL,
    depends_on      text[] NOT NULL,
    clauses         jsonb NOT NULL CHECK (jsonb_typeof(clauses) = 'array'),
    note            text,
    model           text NOT NULL,
    prompt_version  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- n8n adds rules and never rewrites them (ON CONFLICT DO NOTHING): a changed wording is a new key.
GRANT SELECT, INSERT ON criterion_rules TO n8n_app;
