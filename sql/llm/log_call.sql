-- LLM gateway: record one provider request.
-- Parameters: #1 execution id, #2 purpose, #3 provider, #4 model, #5 attempt, #6 http status,
--             #7 outcome, #8 tokens in, #9 tokens out, #10 duration ms, #11 error
INSERT INTO llm_calls (execution_id, purpose, provider, model, attempt, http_status, outcome,
                       tokens_in, tokens_out, duration_ms, error)
VALUES ($1, $2, $3, $4, $5::smallint, $6::integer, $7, $8::integer, $9::integer,
        $10::integer, $11)
RETURNING call_id;
