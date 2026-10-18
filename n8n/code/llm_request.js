// Code node "Build provider request" (LLM gateway). Mode: Run Once for All Items.
//
// The gateway is the only place that knows a provider's wire format, so swapping Gemini for
// another provider is a change here and in one HTTP node's credential. Callers send a
// provider-neutral request: { purpose, model, system, prompt, schema, temperature,
// max_output_tokens, prompt_version }.
//
// The daily request budget is enforced before the call. Gemini's free tier has a
// requests-per-day cap, and spending it on a runaway loop would block the demo for the rest
// of the day.
//
// A caller may send several requests at once (one item each). The HTTP node then sends them
// concurrently, llm_concurrency at a time, and waits between those groups so the account stays
// under llm_rpm (DECISIONS AD29). Every request is checked against the budget on its own.

const GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta/models';

function geminiCall(request) {
  return {
    url: `${GEMINI_BASE}/${encodeURIComponent(request.model)}:generateContent`,
    body: {
      systemInstruction: { parts: [{ text: request.system }] },
      contents: [{ role: 'user', parts: [{ text: request.prompt }] }],
      generationConfig: {
        temperature: request.temperature ?? 0,
        maxOutputTokens: request.max_output_tokens ?? 8192,
        responseMimeType: 'application/json',
        responseJsonSchema: request.schema,
      },
    },
  };
}

function build(request, config, callsToday) {
  const base = {
    purpose: request.purpose,
    prompt_version: request.prompt_version ?? null,
    provider: config.llm_provider,
    model: request.model,
    max_retries: Number(config.llm_max_retries),
  };
  // Run from its own canvas the gateway has no request; say so instead of calling the provider.
  if (!request.model || !request.prompt) {
    return {
      ...base,
      within_budget: false,
      ok: false,
      calls: 0,
      error: 'no request to send: the gateway is called by Workflow A, Workflow B and the warm step '
        + '(run one of those, or pin a { purpose, model, system, prompt, schema } item on "Called by A or B")',
    };
  }
  const budget = Number(config.llm_rpd);
  if (callsToday >= budget) {
    return {
      ...base,
      within_budget: false,
      ok: false,
      calls: 0,
      error: `daily LLM budget reached: ${callsToday} of ${budget} requests used (LLM_RPD)`,
    };
  }
  if (config.llm_provider !== 'gemini') {
    // The gateway's interface is provider-neutral, but only the Gemini branch is wired so far.
    return { ...base, within_budget: false, ok: false, calls: 0, error: `provider ${config.llm_provider} is not wired` };
  }
  return { ...base, within_budget: true, ...geminiCall(request) };
}

// How many requests go out together, and how long to wait between groups to respect llm_rpm.
function pacing(config) {
  const concurrency = Math.max(1, Math.floor(Number(config.llm_concurrency) || 1));
  const rpm = Number(config.llm_rpm);
  const interval = rpm > 0 ? Math.ceil((60000 * concurrency) / rpm) : 0;
  return { batch_size: concurrency, batch_interval_ms: interval };
}

// ---- n8n entry point ----
const config = $('Load app_config').first().json.config;
const callsToday = Number($input.first().json.calls_today ?? 0);
const pace = pacing(config);
const sentMs = Date.now();
return $('Called by A or B').all().map((item, index) => ({
  json: { request_index: index, ...build(item.json, config, callsToday + index), ...pace, sent_ms: sentMs },
}));
