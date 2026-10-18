-- First node of every workflow: all app_config switches as one JSON object (DECISIONS.md AD4).
-- Later nodes read a value as $('Load app_config').first().json.config.email_mode, and so on.
-- (No n8n expression braces in SQL files: the Postgres node evaluates them even inside comments.)
SELECT jsonb_object_agg(key, value) AS config
FROM app_config;
