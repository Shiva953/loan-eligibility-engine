-- LLM gateway, first statement: every app_config switch as one JSON object (as in
-- sql/common/load_config.sql) and the provider requests that count against today's free-tier
-- quota, in one round trip (docs/DECISIONS.md AD27). Gemini resets requests-per-day at midnight
-- Pacific time, so the day is measured in that zone. Rate-limited attempts are rejected by the
-- provider and don't consume the quota.
SELECT (SELECT jsonb_object_agg(key, value) FROM app_config) AS config,
       (SELECT count(*)
        FROM llm_calls
        WHERE outcome <> 'rate_limited'
          AND created_at >= (date_trunc('day', now() AT TIME ZONE 'America/Los_Angeles')
                             AT TIME ZONE 'America/Los_Angeles')) AS calls_today;
