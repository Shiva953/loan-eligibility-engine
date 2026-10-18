// Tests for Workflow B's Code nodes: token claims, cohort requests (S5), verdict collection (S4).
import test from 'node:test';
import assert from 'node:assert/strict';
import { runCodeNode } from './harness.mjs';

const CONFIG = { llm_batch_k: '3', llm_model_match: 'gemini-test' };

function cohort(text, signature, key = `key-${text}`) {
  return { criterion_key: key, criterion_text: text, depends_on: [], signature, pairs: 10 };
}

async function build(cohorts, config = CONFIG) {
  return runCodeNode('build_cohort_requests.js', [{ config, cohorts }]);
}

// --- webhook_claims.js -------------------------------------------------------------------------

const NOW = Math.floor(Date.now() / 1000);
const BATCH = 'B7E1C9A2-3F4D-4A5B-8C6D-7E8F9A0B1C2D';

async function claims(jwtPayload) {
  const [out] = await runCodeNode('webhook_claims.js', [{ jwtPayload }]);
  return out;
}

test('verified claims become a lower-case batch id', async () => {
  const out = await claims({ batch_id: BATCH, iat: NOW, exp: NOW + 300, jti: 'j1' });
  assert.deepEqual(out, { batch_id: BATCH.toLowerCase(), trigger: 'webhook', jti: 'j1' });
});

test('claims that bound replay badly are refused', async () => {
  await assert.rejects(claims({ batch_id: 'not-a-uuid', iat: NOW, exp: NOW + 300 }), /not a UUID/);
  await assert.rejects(claims({ batch_id: BATCH, exp: NOW + 300 }), /iat\/exp/);
  await assert.rejects(claims({ batch_id: BATCH, iat: NOW + 3600, exp: NOW + 3900 }), /future/);
  await assert.rejects(claims({ batch_id: BATCH, iat: NOW, exp: NOW + 3600 }), /10 minutes/);
  await assert.rejects(claims(undefined), /no verified token claims/);
});

// --- build_cohort_requests.js ------------------------------------------------------------------

test('cohorts are packed K to a request, each with an exact-length answer schema', async () => {
  const cohorts = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((s) => cohort('Salaried', `employment_status=${s}`));
  const requests = await build(cohorts);
  assert.deepEqual(requests.map((r) => r.cohort_refs.length), [3, 3, 1]);
  for (const r of requests) {
    const n = r.cohort_refs.length;
    assert.equal(r.schema.properties.verdicts.minItems, n);
    assert.equal(r.schema.properties.verdicts.maxItems, n);
    assert.deepEqual(r.cohort_refs.map((ref) => ref.id), [...Array(n).keys()].map((i) => i + 1));
    assert.equal(r.max_output_tokens, 1024 + 96 * n);
    assert.equal(r.model, 'gemini-test');
    assert.equal(r.purpose, 'judge_cohorts');
    assert.equal(r.temperature, 0);
  }
});

test('a request states each criterion once and lists its cohorts under it', async () => {
  const [request] = await build(
    [
      cohort('Young professionals', 'age=21..24'),
      cohort('High-income individuals', 'monthly_income=100000..'),
      cohort('Young professionals', 'age=25..34'),
    ],
    { ...CONFIG, llm_batch_k: '25' },
  );
  const lines = request.prompt.split('\n');
  assert.deepEqual(lines.filter((l) => l.startsWith('Criterion')), [
    'Criterion A: "High-income individuals"',
    'Criterion B: "Young professionals"',
  ]);
  assert.ok(lines.includes('  1. monthly income ₹1,00,000 or more'));
  assert.ok(lines.includes('  2. age 21–24 years'));
  assert.ok(lines.includes('  3. age 25–34 years'));
  assert.deepEqual(request.cohort_refs[1], { id: 2, criterion_key: 'key-Young professionals', signature: 'age=21..24' });
});

test('signatures read as plain facts: ranges, open ends, exact values, employment', async () => {
  const [request] = await build(
    [cohort('c', 'age=..;credit_score=..749;employment_status=Self-Employed;monthly_income=37000')],
    { ...CONFIG, llm_batch_k: '25' },
  );
  assert.ok(request.prompt.includes(
    '  1. age: any; credit score up to 749; employment: Self-Employed; monthly income ₹37,000',
  ));
});

test('no uncached cohorts means no requests', async () => {
  assert.deepEqual(await build([]), []);
});

// --- attach_cohort_refs.js ---------------------------------------------------------------------

test('the gateway answer gets its request cohort_refs back', async () => {
  const refs = [{ id: 1, criterion_key: 'k', signature: 's' }];
  const other = [{ id: 1, criterion_key: 'k2', signature: 's2' }];
  const out = await runCodeNode('attach_cohort_refs.js', [
    { request_index: 1, ok: true, data: { verdicts: [] }, calls: 1 },
    { request_index: 0, ok: false, calls: 1 },
  ], { nodes: { 'Build cohort requests': [{ cohort_refs: other, prompt: 'q' }, { cohort_refs: refs, prompt: 'p' }] } });
  assert.deepEqual(out[0], { request_index: 1, ok: true, data: { verdicts: [] }, calls: 1, cohort_refs: refs });
  assert.deepEqual(out[1].cohort_refs, other);
});

// --- parse_cohort_verdicts.js ------------------------------------------------------------------

const REFS = [1, 2, 3, 4].map((id) => ({ id, criterion_key: `k${id}`, signature: `s${id}` }));

test('valid verdicts become cache rows; invalid, duplicate and missing answers stay unanswered', async () => {
  const answered = {
    ok: true, calls: 1, tokens_in: 100, tokens_out: 20, model: 'm', prompt_version: 'judge-v1',
    cohort_refs: REFS,
    data: {
      verdicts: [
        { id: 1, verdict: 'eligible', reason: 'x'.repeat(400) },
        { id: 2, verdict: 'maybe', reason: 'not an allowed verdict' },
        { id: 3, verdict: 'ineligible', reason: 'first answer wins' },
        { id: 3, verdict: 'eligible', reason: 'duplicate' },
        { id: '4', verdict: 'uncertain', reason: 'id is not an integer' },
      ],
    },
  };
  const failed = { ok: false, calls: 1, tokens_in: 0, tokens_out: 0, cohort_refs: REFS.slice(0, 2), error: '503' };
  const [out] = await runCodeNode('parse_cohort_verdicts.js', [answered, failed]);
  assert.deepEqual(out.decisions.map((d) => [d.criterion_key, d.signature, d.verdict]), [
    ['k1', 's1', 'eligible'],
    ['k3', 's3', 'ineligible'],
  ]);
  assert.equal(out.decisions[0].reason.length, 300);
  assert.equal(out.decisions[0].model, 'm');
  assert.equal(out.decisions[0].prompt_version, 'judge-v1');
  assert.deepEqual(
    { calls: out.calls, tokens_in: out.tokens_in, tokens_out: out.tokens_out, requests: out.requests,
      failed_requests: out.failed_requests, unanswered: out.unanswered },
    { calls: 2, tokens_in: 100, tokens_out: 20, requests: 2, failed_requests: 1, unanswered: 4 },
  );
});

// --- the rule judge: compile requests and compiled rules ---------------------------------------

const CRITERIA = [
  { criterion_key: 'b-key', criterion_text: 'High-income individuals', depends_on: ['monthly_income'] },
  { criterion_key: 'a-key', criterion_text: 'Salaried applicants', depends_on: ['employment_status', 'monthly_income'] },
];

test('uncompiled criteria become one compile request next to the cohort requests', async () => {
  const requests = await runCodeNode('build_cohort_requests.js', [{ config: CONFIG, cohorts: [cohort('Salaried', 'employment_status=Salaried')], criteria: CRITERIA }]);
  assert.deepEqual(requests.map((r) => r.purpose), ['judge_cohorts', 'compile_criteria']);
  const compile = requests[1];
  assert.equal(compile.prompt_version, 'compile-v1');
  assert.equal(compile.model, 'gemini-test');
  // Sorted by criterion_key, numbered from 1, with the facts each criterion depends on.
  assert.equal(compile.prompt, [
    'Compile each criterion. Answer each id exactly once.',
    '',
    '1. "Salaried applicants" (depends on: employment_status, monthly_income)',
    '2. "High-income individuals" (depends on: monthly_income)',
  ].join('\n'));
  assert.deepEqual(compile.criteria_refs.map((r) => [r.id, r.criterion_key]), [[1, 'a-key'], [2, 'b-key']]);
  assert.equal(compile.schema.properties.rules.minItems, 2);
  assert.equal(compile.schema.properties.rules.maxItems, 2);
  assert.equal(compile.max_output_tokens, 2048 + 384 * 2);
  assert.equal(compile.cohort_refs, undefined);
});

test('a compile answer becomes rules; malformed clauses are dropped, never repaired', async () => {
  const refs = CRITERIA.map((c, i) => ({ id: i + 1, ...c }));
  const good = { verdict: 'ineligible', employment_status: [], age_min: null, age_max: null, credit_score_min: null,
    credit_score_max: 719, monthly_income_min: null, monthly_income_max: null };
  const answer = {
    ok: true, calls: 1, tokens_in: 400, tokens_out: 900, model: 'm', prompt_version: 'compile-v1', criteria_refs: refs,
    data: {
      rules: [
        { id: 1, note: 'stated cut-off', clauses: [
          good,
          { ...good, verdict: 'uncertain' }, // only eligible / ineligible clauses exist
          { ...good, employment_status: ['Freelancer'] }, // unknown employment
          { ...good, age_min: 'twenty' }, // a bound must be a number or null
        ] },
      ],
    },
  };
  const failedCompile = { ok: false, calls: 1, tokens_in: 0, tokens_out: 0, criteria_refs: refs, error: '503' };
  const [out] = await runCodeNode('parse_cohort_verdicts.js', [answer, failedCompile]);
  assert.equal(out.decisions.length, 0);
  assert.deepEqual(out.rules, [{
    criterion_key: 'b-key', criterion_text: 'High-income individuals', depends_on: ['monthly_income'],
    clauses: [good], note: 'stated cut-off', model: 'm', prompt_version: 'compile-v1',
  }]);
  // Rule 2 was not answered, and the failed request's two criteria stay uncompiled too.
  assert.equal(out.unanswered, 3);
  assert.equal(out.failed_requests, 1);
});

test('a compile request keeps its criteria_refs', async () => {
  const refs = [{ id: 1, criterion_key: 'k', criterion_text: 't', depends_on: [] }];
  const [out] = await runCodeNode('attach_cohort_refs.js', [{ request_index: 0, ok: true, data: { rules: [] } }], {
    nodes: { 'Build cohort requests': [{ criteria_refs: refs }] },
  });
  assert.deepEqual(out, { request_index: 0, ok: true, data: { rules: [] }, criteria_refs: refs });
});

// --- request size (K) planning, per model ----------------------------------------------------

const many = Array.from({ length: 19 }, (_, i) => cohort('Salaried', `employment_status=E${String(i).padStart(2, '0')}`, `key-${i}`));
const sizes = (requests) => requests.filter((r) => r.purpose === 'judge_cohorts').map((r) => r.cohort_refs.length);
const TOKEN_BOUND = { calls: 200, l0_ms: 400, ms_per_token: 2, r2: 0.9 }; // a fast paid model: latency grows with output
const FLAT = { calls: 343, l0_ms: 11072, ms_per_token: 5.37, r2: 0.01 }; // Gemini free tier, measured

async function planned(config, latency) {
  return runCodeNode('build_cohort_requests.js', [{ config: { llm_batch_k: '25', llm_model_match: 'm', ...config }, llm_latency: latency, cohorts: many }]);
}

test('one request at a time, K is the cap and the requests are exactly as before', async () => {
  assert.deepEqual(sizes(await planned({ llm_concurrency: '1' }, TOKEN_BOUND)), [19]);
  assert.deepEqual(sizes(await planned({ llm_batch_k: '8', llm_concurrency: '1' }, TOKEN_BOUND)), [8, 8, 3]);
});

test('a flat per-request latency keeps the fewest requests, even with parallel slots', async () => {
  assert.deepEqual(sizes(await planned({ llm_concurrency: '4' }, FLAT)), [19]);
  assert.deepEqual(sizes(await planned({ llm_concurrency: '4' }, null)), [19]);
});

test('with parallel slots and output-bound latency, requests shrink to fill the slots evenly', async () => {
  const four = sizes(await planned({ llm_concurrency: '4' }, TOKEN_BOUND));
  assert.equal(four.length, 4);
  assert.deepEqual(four, [5, 5, 5, 4]);
  // too few calls in the ledger to trust the fit: treated as flat
  assert.deepEqual(sizes(await planned({ llm_concurrency: '4' }, { ...TOKEN_BOUND, calls: 5 })), [19]);
});
