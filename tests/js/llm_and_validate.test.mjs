// Tests for the LLM gateway's Code nodes and Workflow A's validator (S1 compile + S6 guard).
import test from 'node:test';
import assert from 'node:assert/strict';
import { runCodeNode } from './harness.mjs';

const CONFIG = {
  extraction_confidence_min: '0.7',
  llm_provider: 'gemini',
  llm_rpd: '500',
  llm_max_retries: '3',
};

// --- llm_request.js --------------------------------------------------------------------------

const REQUEST = {
  purpose: 'extract_products', model: 'gemini-3.5-flash-lite', system: 'sys', prompt: 'p',
  schema: { type: 'object' }, temperature: 0, max_output_tokens: 100, prompt_version: 'v',
};

async function buildWith(callsToday, config = CONFIG, request = REQUEST) {
  const [out] = await runCodeNode('llm_request.js', [{ calls_today: callsToday }], {
    nodes: { 'Called by A or B': request, 'Load app_config': { config } },
  });
  return out;
}

test('several requests go out together, each checked against the budget, paced under llm_rpm', async () => {
  const items = await runCodeNode('llm_request.js', [{ calls_today: 498 }], {
    nodes: {
      'Called by A or B': [REQUEST, { ...REQUEST, prompt: 'q' }, { ...REQUEST, prompt: 'r' }],
      'Load app_config': { config: { ...CONFIG, llm_concurrency: '2', llm_rpm: '10' } },
    },
  });
  assert.deepEqual(items.map((i) => i.request_index), [0, 1, 2]);
  assert.deepEqual(items.map((i) => i.within_budget), [true, true, false]); // 498, 499, then 500 of 500
  assert.equal(items[0].batch_size, 2);
  assert.equal(items[0].batch_interval_ms, 12000); // 2 requests every 12 s = 10 a minute
});

test('run on its own, with no request, the gateway calls nothing and says why', async () => {
  const out = await buildWith(0, CONFIG, {});
  assert.equal(out.within_budget, false);
  assert.equal(out.calls, 0);
  assert.match(out.error, /no request to send/);
});

test('gateway builds a Gemini structured-output request', async () => {
  const out = await buildWith(3);
  assert.equal(out.within_budget, true);
  assert.equal(out.url, 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent');
  assert.equal(out.body.generationConfig.responseMimeType, 'application/json');
  assert.deepEqual(out.body.generationConfig.responseJsonSchema, { type: 'object' });
  assert.equal(out.body.systemInstruction.parts[0].text, 'sys');
});

test('gateway refuses once the daily budget is spent, and for unwired providers', async () => {
  const spent = await buildWith(500);
  assert.equal(spent.within_budget, false);
  assert.match(spent.error, /daily LLM budget reached: 500 of 500/);
  const groq = await buildWith(0, { ...CONFIG, llm_provider: 'groq' });
  assert.equal(groq.within_budget, false);
  assert.match(groq.error, /groq is not wired/);
});

// --- llm_response.js -------------------------------------------------------------------------

const BUILT = { ...REQUEST, provider: 'gemini', url: 'u', body: {}, max_retries: 3 };

// Pass 0 reads what "Within daily budget?" let through; a retry pass reads what "Back off" resent.
async function parse(response, runIndex = 0) {
  const runs = Array.from({ length: Math.max(1, runIndex) }, () => [[BUILT]]);
  const [out] = await runCodeNode('llm_response.js', [response], {
    nodes: { 'Within daily budget?': { runs: [[[BUILT]]] }, 'Back off': { runs } },
    runIndex,
  });
  return out;
}

const geminiOk = (text, finishReason = 'STOP') => ({
  statusCode: 200,
  body: {
    candidates: [{ finishReason, content: { parts: [{ text }] } }],
    usageMetadata: { promptTokenCount: 120, candidatesTokenCount: 30, thoughtsTokenCount: 5 },
  },
});

test('200 with valid JSON is ok and counts thinking tokens as output', async () => {
  const out = await parse(geminiOk('{"products":[]}'));
  assert.equal(out.ok, true);
  assert.deepEqual(out.data, { products: [] });
  assert.equal(out.tokens_in, 120);
  assert.equal(out.tokens_out, 35);
  assert.equal(out.attempt, 1);
});

test('truncated or non-JSON output is an error without retry', async () => {
  const truncated = await parse(geminiOk('{"products":[', 'MAX_TOKENS'));
  assert.equal(truncated.ok, false);
  assert.equal(truncated.retry, false);
  assert.match(truncated.error, /MAX_TOKENS/);
  const junk = await parse(geminiOk('not json'));
  assert.match(junk.error, /not valid JSON/);
});

test('429 retries using the RetryInfo delay; 503 backs off exponentially', async () => {
  const limited = await parse({
    statusCode: 429,
    body: { error: { message: 'quota', details: [{ '@type': 'type.googleapis.com/google.rpc.RetryInfo', retryDelay: '17s' }] } },
  });
  assert.equal(limited.retry, true);
  assert.equal(limited.outcome, 'rate_limited');
  assert.equal(limited.wait_seconds, 17);
  const busy = await parse({ statusCode: 503, body: { error: { message: 'overloaded' } } }, 1);
  assert.equal(busy.retry, true);
  assert.equal(busy.wait_seconds, 8);
  assert.equal(busy.attempt, 2);
});

test('responses pair with the requests of their own pass, and record how long the pass took', async () => {
  const sent = [{ ...BUILT, request_index: 0, sent_ms: Date.now() - 5000 }, { ...BUILT, request_index: 1, sent_ms: Date.now() - 5000 }];
  const out = await runCodeNode('llm_response.js', [geminiOk('{"a":1}'), { statusCode: 429, body: {} }], {
    nodes: { 'Within daily budget?': { runs: [[sent]] }, 'Back off': { runs: [] } },
  });
  assert.deepEqual(out.map((o) => [o.request_index, o.ok, o.retry]), [[0, true, false], [1, false, true]]);
  assert.ok(out[0].duration_ms >= 5000 && out[0].duration_ms < 60000);
});

test('requests to retry wait together, as long as the longest delay', async () => {
  const out = await runCodeNode('restore_response.js', [], {
    nodes: { 'Parse provider response': [
      { request_index: 0, ok: true, retry: false },
      { request_index: 1, retry: true, wait_seconds: 4 },
      { request_index: 2, retry: true, wait_seconds: 17 },
    ] },
  });
  assert.deepEqual(out.map((o) => o.wait_seconds), [undefined, 17, 17]);
  assert.ok(out[1].sent_ms >= Date.now() + 16000);
});

test('the result lists every request once, in order, from whichever pass finished it', async () => {
  const refused = { request_index: 3, ok: false, error: 'daily LLM budget reached', calls: 0 };
  const answered = (i, attempt) => ({ request_index: i, ok: true, data: { i }, attempt, tokens_in: 1, tokens_out: 2 });
  const out = await runCodeNode('llm_result.js', [], {
    nodes: {
      'Within daily budget?': { runs: [[[], [refused]]] },
      // pass 0 finished requests 0 and 2; request 1 was retried and finished in pass 1
      'Retry?': { runs: [[[{ request_index: 1 }], [answered(2, 1), answered(0, 1)]], [[], [answered(1, 2)]]] },
    },
  });
  assert.deepEqual(out.map((o) => [o.request_index, o.ok, o.calls]), [[0, true, 1], [1, true, 2], [2, true, 1], [3, false, 0]]);
});

test('retries stop at max_retries; 400 never retries', async () => {
  const last = await parse({ statusCode: 503, body: {} }, 2);
  assert.equal(last.retry, false);
  assert.equal(last.ok, false);
  const bad = await parse({ statusCode: 400, body: { error: { message: 'bad schema' } } });
  assert.equal(bad.retry, false);
  assert.equal(bad.error, 'bad schema');
});

// --- validate_products.js --------------------------------------------------------------------

const full = { interest_rate: 1, age: 1, credit_score: 1, income: 1, employment: 1 };
const base = {
  lender: 'Kotak Mahindra Bank', product_name: 'Kotak Personal Loan',
  interest_rate_min_pct: 10.99, interest_rate_max_pct: null,
  min_age: 21, max_age: 60, min_credit_score: null, credit_score_kind: 'not_stated',
  income_thresholds: [], employment_allowed: [], other_criteria: [], confidence: full, evidence: 'e',
};

async function validate(products, threshold = '0.7') {
  const [out] = await runCodeNode('validate_products.js', [{ ok: true, data: { products }, calls: 1, tokens_in: 10, tokens_out: 5 }], {
    nodes: {
      'Loop over sources': { source_id: 3, page_urls: ['https://kotak/eligibility'] },
      'Hash page text': { content_hash: 'abc' },
      'Load app_config': { config: { extraction_confidence_min: threshold } },
    },
  });
  return out;
}

test('income tiers: the general public\'s tier is the filter, conditional tiers become caveats', async () => {
  const { products: [p] } = await validate([{
    ...base,
    income_thresholds: [
      { amount: 25000, period: 'monthly', employment: [], condition: 'for Kotak salary account holders' },
      { amount: 30000, period: 'monthly', employment: [], condition: 'for non Kotak salary account holders' },
      { amount: 20000, period: 'monthly', employment: [], condition: 'for Kotak Mahindra Bank employees' },
    ],
  }]);
  assert.equal(p.min_monthly_income, 30000); // employees and salary-account holders are concessions
  const caveats = p.soft_criteria.filter((s) => s.origin === 'income_tier');
  assert.equal(caveats.length, 3);
  assert.ok(caveats.every((s) => s.class === 'unknown_field'));
  assert.ok(caveats.some((s) => s.text === 'Minimum income ₹30,000/month for non Kotak salary account holders'));
});

test('annual income is converted; employment-specific tiers become compilable rules', async () => {
  const { products: [p] } = await validate([{
    ...base,
    income_thresholds: [
      { amount: 15000, period: 'monthly', employment: ['Salaried'], condition: null },
      { amount: 500000, period: 'annual', employment: ['Self-Employed'], condition: null },
    ],
  }]);
  assert.equal(p.min_monthly_income, 15000);
  const rule = p.soft_criteria.find((s) => s.rule?.field === 'monthly_income');
  assert.equal(rule.class, 'compilable');
  assert.deepEqual(rule.rule, { field: 'monthly_income', op: '>=', value: 41667, when: { employment_status: ['Self-Employed'] } });
});

test('a "monthly" amount that looks annual loses confidence and stops filtering (S6)', async () => {
  const { products: [p], issues } = await validate([{
    ...base, income_thresholds: [{ amount: 600000, period: 'monthly', employment: [], condition: null }],
  }]);
  assert.equal(p.min_monthly_income, null);
  assert.equal(p.field_confidence.min_monthly_income, 0.4);
  assert.ok(p.soft_criteria.some((s) => s.origin === 'low_confidence' && s.depends_on[0] === 'monthly_income'));
  assert.ok(issues.some((i) => /looks annual/.test(i)));
});

test('preferred credit score ranks; a stated minimum filters', async () => {
  const { products: [preferred] } = await validate([{ ...base, min_credit_score: 750, credit_score_kind: 'preferred' }]);
  assert.equal(preferred.min_credit_score, null);
  assert.equal(preferred.soft_criteria[0].rule.effect, 'rank');
  const { products: [minimum] } = await validate([{ ...base, min_credit_score: 720, credit_score_kind: 'minimum' }]);
  assert.equal(minimum.min_credit_score, 720);
});

test('S6: low-confidence fields become fuzzy criteria instead of filters', async () => {
  const { products: [p] } = await validate([{
    ...base,
    min_credit_score: 720,
    credit_score_kind: 'minimum',
    employment_allowed: ['Salaried'],
    confidence: { ...full, credit_score: 0.5, age: 0.4 },
  }]);
  assert.equal(p.min_credit_score, null);
  assert.equal(p.min_age, null);
  assert.equal(p.max_age, null);
  assert.deepEqual(p.allowed_employment, ['Salaried']);
  const fuzzy = p.soft_criteria.filter((s) => s.class === 'fuzzy').map((s) => s.depends_on[0]).sort();
  assert.deepEqual(fuzzy, ['age', 'credit_score']);
});

test('ranges, swaps, vocabulary and classification fixes', async () => {
  const { products: [p], issues } = await validate([{
    ...base,
    interest_rate_min_pct: 21, interest_rate_max_pct: 10.5,
    min_age: 70, max_age: 21,
    employment_allowed: ['Salaried', 'Self-Employed', 'Business'],
    other_criteria: [
      { text: 'Self-employed professionals', class: 'fuzzy', depends_on: ['employment_status'] },
      { text: 'Must work at an MNC', class: 'fuzzy', depends_on: [] },
      { text: '2 years of work experience', class: 'unknown_field', depends_on: ['age'] },
    ],
  }]);
  assert.deepEqual([p.interest_rate_min, p.interest_rate_max], [10.5, 21]);
  assert.deepEqual([p.min_age, p.max_age], [null, null]);
  assert.equal(p.allowed_employment, null);
  const byText = Object.fromEntries(p.soft_criteria.map((s) => [s.text, s]));
  assert.equal(byText['Self-employed professionals'].class, 'fuzzy');
  assert.equal(byText['Must work at an MNC'].class, 'unknown_field');
  assert.deepEqual(byText['2 years of work experience'].depends_on, []);
  assert.ok(issues.some((i) => /swapped/.test(i)));
});

test('criteria_hash ignores confidence and evidence, but tracks rule changes', async () => {
  const a = (await validate([{ ...base, evidence: 'one', confidence: { ...full, interest_rate: 0.9 } }])).products[0];
  const b = (await validate([{ ...base, evidence: 'two', confidence: { ...full, interest_rate: 0.8 } }])).products[0];
  const c = (await validate([{ ...base, max_age: 58 }])).products[0];
  assert.equal(a.criteria_hash, b.criteria_hash);
  assert.notEqual(a.criteria_hash, c.criteria_hash);
  assert.equal(a.source_url, 'https://kotak/eligibility');
});

test('duplicates keep the more complete entry; nameless products are dropped', async () => {
  const out = await validate([
    { ...base, min_age: null, max_age: null },
    { ...base },
    { ...base, lender: '', product_name: 'x' },
  ]);
  assert.equal(out.products_found, 1);
  assert.equal(out.products[0].min_age, 21);
  assert.equal(out.products_dropped, 2);
  assert.equal(out.llm_calls, 1);
  assert.equal(out.content_hash, 'abc');
});

test('one lender under its acronym and its full name is one product; lookalike initials are not', async () => {
  const named = (lender, product_name, extra = {}) => ({ ...base, lender, product_name, ...extra });
  const out = await validate([
    named('IIFL', 'IIFL Personal Loan', { min_age: null }),
    named('India Infoline Finance Ltd.', 'India Infoline Finance Ltd. Personal Loan'),
    named('Punjab National Bank', 'PNB Personal Loan'),
    named('PNB', 'Punjab National Bank Personal Loan'),
    named('Home Credit', 'Home Credit Personal Loan'),
    named('Hero Credit', 'Hero Credit Personal Loan'),
    named('Edelweiss', 'Edelweiss Salaried Personal Loan'),
    named('Edelweiss', 'Edelweiss Personal Loan'),
  ]);
  const lenders = out.products.map((p) => p.lender).sort();
  assert.deepEqual(lenders, ['Edelweiss', 'Edelweiss', 'Hero Credit', 'Home Credit', 'India Infoline Finance Ltd.', 'Punjab National Bank']);
  assert.equal(out.products_dropped, 2);
  assert.ok(out.issues.some((i) => i.includes('same product as')));
});

test("an income tier for the bank's own employees or salary-account holders doesn't lower the floor", async () => {
  const { products: [p] } = await validate([{ ...base, income_thresholds: [
    { amount: 20000, period: 'monthly', employment: [], condition: 'Kotak Mahindra Bank Employees' },
    { amount: 25000, period: 'monthly', employment: [], condition: 'Kotak Mahindra Bank Salary Account holder' },
    { amount: 30000, period: 'monthly', employment: [], condition: 'non Kotak Mahindra Bank Salary Account holder' },
  ] }]);
  assert.equal(p.min_monthly_income, 30000); // the general public's tier
  assert.ok(p.soft_criteria.some((c) => c.text.includes('₹20,000') && c.class === 'unknown_field'));
});
