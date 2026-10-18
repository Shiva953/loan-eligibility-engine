// Send provider-neutral LLM requests to Gemini using the LLM gateway's own Code-node logic.
//
// Why not call the n8n gateway: an experiment needs a few hundred requests, and starting n8n
// once per request would measure container start-up, not the strategy. This runner executes
// the gateway's llm_request.js (request body) and llm_response.js (parsing, retry decision)
// through the same harness the tests use. The wire format, parsing and backoff are therefore
// identical to production; only the orchestration differs.
//
// It also paces requests (BENCH_RPM, default 10 per minute) and caches successful responses
// on disk keyed by the exact request, so re-running an analysis never spends quota twice
// (BENCH_NO_CACHE=1 bypasses the cache; a request's cache_salt caches a deliberate re-ask).
//
// Usage: node bench/llm_runner.mjs <requests.json> <results.jsonl>
//        node bench/llm_runner.mjs <requests.json> --count-uncached   (prints how many would be sent)
//   requests.json: { "config": {app_config}, "requests": [{ "id": ..., purpose, model, system,
//                    prompt, schema, temperature, max_output_tokens }] }
//   results.jsonl: one { id, ok, data, error, tokens_in, tokens_out, attempts, ms, cached } per line
import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { runCodeNode, REPO_ROOT } from '../tests/js/harness.mjs';

const [input, output] = process.argv.slice(2);
const { config, requests } = JSON.parse(readFileSync(input, 'utf8'));
const key = process.env.GEMINI_API_KEY;
const rpm = Number(process.env.BENCH_RPM ?? 10);
const useCache = process.env.BENCH_NO_CACHE !== '1';
const cacheDir = path.join(REPO_ROOT, 'bench', 'llm_cache');
mkdirSync(cacheDir, { recursive: true });

// cache_salt (optional) caches a deliberate re-ask of the same question separately, so a
// self-consistency run is cached too without overwriting the first answer.
function cacheFileFor(req) {
  const keyParts = [req.model, req.system, req.prompt, req.schema, req.temperature ?? 0];
  if (req.cache_salt) keyParts.push(req.cache_salt);
  return path.join(cacheDir, `${createHash('sha256').update(JSON.stringify(keyParts)).digest('hex')}.json`);
}

// Budget checks count only what would really be sent: cached answers cost nothing.
if (output === '--count-uncached') {
  console.log(useCache ? requests.filter((req) => !existsSync(cacheFileFor(req))).length : requests.length);
  process.exit(0);
}
writeFileSync(output, '');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let lastStart = 0;

async function call(built) {
  const wait = lastStart + 60000 / rpm - Date.now();
  if (wait > 0) await sleep(wait);
  lastStart = Date.now();
  try {
    const res = await fetch(built.url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-goog-api-key': key },
      body: JSON.stringify(built.body),
    });
    const body = await res.json().catch(() => ({}));
    return { statusCode: res.status, body, headers: { 'retry-after': res.headers.get('retry-after') } };
  } catch (err) {
    return { statusCode: 0, error: { message: String(err.message ?? err) } };
  }
}

for (const req of requests) {
  const cacheFile = cacheFileFor(req);
  if (useCache && existsSync(cacheFile)) {
    const cached = JSON.parse(readFileSync(cacheFile, 'utf8'));
    appendFileSync(output, `${JSON.stringify({ ...cached, id: req.id, cached: true })}\n`);
    continue;
  }
  const [built] = await runCodeNode('llm_request.js', [{ calls_today: 0 }], {
    nodes: { 'Called by A or B': req, 'Load app_config': { config } },
  });
  const started = Date.now();
  const attempts = [];
  let parsed = { ok: false, error: 'not attempted' };
  for (let runIndex = 0; ; runIndex += 1) {
    const t0 = Date.now();
    const response = await call(built);
    [parsed] = await runCodeNode('llm_response.js', [response], {
      nodes: { 'Build provider request': built },
      runIndex,
    });
    attempts.push({ status: response.statusCode, outcome: parsed.outcome, ms: Date.now() - t0 });
    if (!parsed.retry) break;
    process.stderr.write(`  ${req.id}: ${parsed.outcome} (HTTP ${response.statusCode}), retry in ${parsed.wait_seconds}s\n`);
    await sleep(parsed.wait_seconds * 1000);
  }
  const result = {
    ok: parsed.ok,
    data: parsed.ok ? parsed.data : null,
    error: parsed.ok ? null : parsed.error,
    tokens_in: parsed.tokens_in ?? 0,
    tokens_out: parsed.tokens_out ?? 0,
    attempts,
    ms: Date.now() - started,
    model: req.model,
  };
  if (result.ok && useCache) writeFileSync(cacheFile, JSON.stringify(result));
  appendFileSync(output, `${JSON.stringify({ ...result, id: req.id, cached: false })}\n`);
  process.stderr.write(`${req.id}: ${result.ok ? 'ok' : `FAILED ${result.error}`} (${attempts.length} attempt(s), ${result.ms} ms)\n`);
}
