// Code node "Parse provider response" (LLM gateway). Mode: Run Once for All Items.
//
// Decides one of three outcomes for each provider response:
//   * ok: valid JSON matching the schema the caller asked for;
//   * retry: rate limited (429), server error (5xx) or network failure, while attempts remain.
//     The wait comes from the provider's RetryInfo or Retry-After when given, otherwise it is
//     exponential (4s, 8s, 16s ... capped at 60s);
//   * error: anything else, or retries exhausted.
// This node runs once per attempt (a pass over every request still in flight), so the attempt
// number is n8n's run index + 1. Pass 0 answers what "Within daily budget?" let through, later
// passes what "Back off" sent again; the HTTP node keeps item order, so response i belongs to
// request i of the same pass. duration_ms runs from the pass's start to its last response.

function retryDelaySeconds(response, attempt) {
  const details = response.body?.error?.details ?? [];
  const info = details.find((d) => String(d['@type'] ?? '').endsWith('RetryInfo'));
  const fromBody = info ? parseFloat(String(info.retryDelay)) : NaN;
  const fromHeader = parseFloat(response.headers?.['retry-after'] ?? '');
  const hinted = Number.isFinite(fromBody) ? fromBody : fromHeader;
  if (Number.isFinite(hinted) && hinted > 0) return Math.min(Math.ceil(hinted), 60);
  return Math.min(2 ** (attempt + 1), 60);
}

function parseGemini(body) {
  const usage = body.usageMetadata ?? {};
  const tokens = {
    tokens_in: usage.promptTokenCount ?? 0,
    tokens_out: (usage.candidatesTokenCount ?? 0) + (usage.thoughtsTokenCount ?? 0),
  };
  if (body.promptFeedback?.blockReason) {
    return { ...tokens, error: `prompt blocked: ${body.promptFeedback.blockReason}` };
  }
  const candidate = body.candidates?.[0];
  if (!candidate) return { ...tokens, error: 'no candidates in response' };
  if (candidate.finishReason && candidate.finishReason !== 'STOP') {
    return { ...tokens, error: `generation stopped: ${candidate.finishReason}` };
  }
  const text = (candidate.content?.parts ?? []).map((part) => part.text ?? '').join('');
  try {
    return { ...tokens, data: JSON.parse(text) };
  } catch (err) {
    return { ...tokens, error: `response was not valid JSON: ${err.message}` };
  }
}

function decide(request, response, attempt) {
  const status = response.statusCode ?? 0;
  const common = {
    request_index: request.request_index,
    batch_size: request.batch_size,
    batch_interval_ms: request.batch_interval_ms,
    purpose: request.purpose,
    prompt_version: request.prompt_version,
    provider: request.provider,
    model: request.model,
    url: request.url,
    body: request.body,
    max_retries: request.max_retries,
    attempt,
    http_status: status,
  };
  if (status === 200) {
    const parsed = parseGemini(response.body ?? {});
    if (parsed.error) return { ...common, ...parsed, ok: false, retry: false, outcome: 'error' };
    return { ...common, ...parsed, ok: true, retry: false, outcome: 'ok', error: null };
  }
  const retryable = status === 0 || status === 429 || status >= 500;
  const message =
    response.body?.error?.message ?? response.error?.message ?? `HTTP ${status || 'request failed'}`;
  const outcome = status === 429 ? 'rate_limited' : 'error';
  if (retryable && attempt < request.max_retries) {
    return {
      ...common, ok: false, retry: true, outcome, tokens_in: 0, tokens_out: 0,
      wait_seconds: retryDelaySeconds(response, attempt), error: message,
    };
  }
  return { ...common, ok: false, retry: false, outcome, tokens_in: 0, tokens_out: 0, error: message };
}

// ---- n8n entry point ----
const pass = $runIndex;
const sent = pass === 0 ? $('Within daily budget?').all(0, 0) : $('Back off').all(0, pass - 1);
const now = Date.now();
return $input.all().map((response, i) => {
  const request = sent[i].json;
  const outcome = decide(request, response.json, pass + 1);
  return { json: { ...outcome, duration_ms: Math.max(0, now - (request.sent_ms ?? now)) } };
});
