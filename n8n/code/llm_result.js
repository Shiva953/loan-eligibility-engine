// Code node "Return result" (LLM gateway). Mode: Run Once for All Items.
//
// The gateway's only output: one item per request, in request order, in the same shape
// whether the call succeeded, failed or was refused by the budget guard. Callers branch on `ok`,
// and record `calls` and the tokens in their own metrics. The request URL and body never leave
// the gateway.
//
// A request ends either at "Within daily budget?" (refused) or at "Retry?" (answered, or out of
// retries), in whichever pass it finished. This node runs once for each pass that finished
// something, and n8n hands the caller only its last run, so every run reads all of them.

const MAX_PASSES = 20;

function result(item) {
  return {
    request_index: item.request_index ?? 0,
    ok: item.ok === true,
    data: item.ok === true ? item.data : null,
    error: item.ok === true ? null : item.error ?? 'unknown error',
    purpose: item.purpose,
    prompt_version: item.prompt_version ?? null,
    provider: item.provider,
    model: item.model,
    calls: item.attempt ?? item.calls ?? 0,
    tokens_in: item.tokens_in ?? 0,
    tokens_out: item.tokens_out ?? 0,
  };
}

// Items one node put on one branch in one run; null once that run doesn't exist.
function ended(node, branch, run) {
  try {
    return $(node).all(branch, run).map((item) => item.json);
  } catch (error) {
    return /not found|hasn't been executed/.test(String(error?.message)) ? null : [];
  }
}

function finished() {
  const byRequest = new Map();
  for (const item of ended('Within daily budget?', 1, 0) ?? []) byRequest.set(item.request_index, item);
  for (let run = 0; run < MAX_PASSES; run += 1) {
    const items = ended('Retry?', 1, run);
    if (items === null) break;
    for (const item of items) byRequest.set(item.request_index, item);
  }
  return [...byRequest.values()].sort((a, b) => (a.request_index ?? 0) - (b.request_index ?? 0));
}

// ---- n8n entry point ----
return finished().map((item) => ({ json: result(item) }));
