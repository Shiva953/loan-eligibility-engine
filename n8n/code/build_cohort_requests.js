// Code node "Build cohort requests" (Workflow B). Mode: Run Once for All Items.
//
// Fuzzy criteria get two independent judges (docs/EXPERIMENTS.md, LLM stage):
//   1. Cohort judge (S3-S5): the uncached cohorts are packed K to a request, so a request costs
//      one gateway call whatever the number of users behind it. Cohorts are sorted by criterion,
//      so each request states a criterion's wording once and lists its cohorts under it. Each
//      cohort is described only by the banded facts its criterion depends on, so one verdict
//      holds for every user in the cohort.
//   2. Rule judge: every criterion not compiled yet goes into one compile request. The LLM
//      writes clauses tagged eligible / ineligible over the exact facts, and leaves everything
//      else uncertain; SQL applies them to every user (write_matches.sql).
// The prompts here are the ones the bench validated against the oracle; bench/llm_experiments.py
// builds its requests through this file, so the two can't drift.
//
// Input:  one item { config (app_config), llm_latency (the match model's fitted latency),
//                    cohorts: [{ criterion_key, criterion_text, depends_on, signature, pairs }],
//                    criteria: [{ criterion_key, criterion_text, depends_on }] }
// Output: one item per request, provider-neutral for the LLM gateway, plus cohort_refs
//         (question id -> criterion_key + signature) or criteria_refs (rule id -> criterion)
//         for mapping the answers back.

const PROMPT_VERSION = 'judge-v1';
const COMPILE_PROMPT_VERSION = 'compile-v1';
const COMPILE_MAX_CRITERIA = 25;

const COMPILE_SYSTEM = `You turn a lender's qualitative eligibility criterion into explicit conditions on the applicant facts it depends on, so a database can apply it without asking again. Facts: age (years), credit_score (300-900 CIBIL), employment_status (exactly one of Salaried, Self-Employed, Business), monthly_income (INR per month).
For each criterion give a list of clauses, each with a verdict:
- eligible: the applicant profiles it covers clearly satisfy the criterion;
- ineligible: the applicant profiles it covers clearly do not satisfy it.
Every profile no clause covers is uncertain. A clause covers a profile when every condition in it holds; leave a bound null when it does not restrict, and leave employment_status empty when any employment qualifies. Write a clause only when the answer would be the same for every applicant it covers. Do not invent cut-offs the wording does not imply: when the criterion depends on something the facts do not show, leave those profiles uncertain.`;

const SYSTEM = `You decide whether loan applicants meet a lender's qualitative eligibility criterion. Each question gives one criterion and an applicant profile. When a fact is given as a range, the applicant's value lies somewhere in that range: answer for the whole range. Facts that are not mentioned are unknown; do not assume them.
Answer each question with exactly one verdict:
- eligible: the profile clearly satisfies the criterion;
- ineligible: the profile clearly does not satisfy it;
- uncertain: the answer depends on something not given, or the range includes applicants who would get different answers.
Judge only the stated criterion, not other lending rules. Give a reason of at most 15 words.`;

const LABELS = {
  age: (lo, hi) => (lo && hi ? `age ${lo}–${hi} years` : lo ? `age ${lo} years or older` : `age up to ${hi} years`),
  credit_score: (lo, hi) => (lo && hi ? `credit score ${lo}–${hi}` : lo ? `credit score ${lo} or higher` : `credit score up to ${hi}`),
  monthly_income: (lo, hi) => {
    const r = (v) => `₹${Number(v).toLocaleString('en-IN')}`;
    return lo && hi ? `monthly income ${r(lo)}–${r(hi)}` : lo ? `monthly income ${r(lo)} or more` : `monthly income up to ${r(hi)}`;
  },
};

const EXACT = {
  age: (v) => `age ${v} years`,
  credit_score: (v) => `credit score ${v}`,
  monthly_income: (v) => `monthly income ₹${Number(v).toLocaleString('en-IN')}`,
};

// "age=21..24;employment_status=Salaried" -> "age 21–24 years; employment: Salaried"
function describe(signature) {
  return signature
    .split(';')
    .filter(Boolean)
    .map((part) => {
      const [field, value] = part.split('=');
      if (field === 'employment_status') return `employment: ${value}`;
      if (!value.includes('..')) return EXACT[field](value);
      const [lo, hi] = value.split('..');
      if (!lo && !hi) return `${field.replace('_', ' ')}: any`;
      return LABELS[field](lo, hi);
    })
    .join('; ');
}

function schemaFor(count) {
  return {
    type: 'object',
    properties: {
      verdicts: {
        type: 'array',
        minItems: count,
        maxItems: count,
        items: {
          type: 'object',
          properties: {
            id: { type: 'integer' },
            verdict: { type: 'string', enum: ['eligible', 'ineligible', 'uncertain'] },
            reason: { type: 'string' },
          },
          required: ['id', 'verdict', 'reason'],
          additionalProperties: false,
        },
      },
    },
    required: ['verdicts'],
    additionalProperties: false,
  };
}

// Key order matters: the bench's cache key is the serialised schema.
function compileSchemaFor(count) {
  const bound = { type: ['number', 'null'] };
  const clause = {
    type: 'object',
    additionalProperties: false,
    properties: {
      verdict: { type: 'string', enum: ['eligible', 'ineligible'] },
      employment_status: { type: 'array', items: { type: 'string', enum: ['Salaried', 'Self-Employed', 'Business'] } },
      age_min: bound,
      age_max: bound,
      credit_score_min: bound,
      credit_score_max: bound,
      monthly_income_min: bound,
      monthly_income_max: bound,
    },
    required: ['verdict', 'employment_status', 'age_min', 'age_max', 'credit_score_min',
      'credit_score_max', 'monthly_income_min', 'monthly_income_max'],
  };
  return {
    type: 'object',
    required: ['rules'],
    additionalProperties: false,
    properties: {
      rules: {
        type: 'array',
        minItems: count,
        maxItems: count,
        items: {
          type: 'object',
          required: ['id', 'clauses', 'note'],
          additionalProperties: false,
          properties: { id: { type: 'integer' }, clauses: { type: 'array', items: clause }, note: { type: 'string' } },
        },
      },
    },
  };
}

function compileRequests(criteria, config) {
  const sorted = [...criteria].sort((a, b) => (a.criterion_key < b.criterion_key ? -1 : a.criterion_key > b.criterion_key ? 1 : 0));
  const requests = [];
  for (let start = 0; start < sorted.length; start += COMPILE_MAX_CRITERIA) {
    const chunk = sorted.slice(start, start + COMPILE_MAX_CRITERIA);
    const lines = ['Compile each criterion. Answer each id exactly once.', ''];
    chunk.forEach((c, i) => lines.push(`${i + 1}. "${c.criterion_text}" (depends on: ${c.depends_on.join(', ')})`));
    requests.push({
      purpose: 'compile_criteria',
      prompt_version: COMPILE_PROMPT_VERSION,
      model: config.llm_model_match,
      temperature: 0,
      max_output_tokens: 2048 + 384 * chunk.length,
      system: COMPILE_SYSTEM,
      prompt: lines.join('\n'),
      schema: compileSchemaFor(chunk.length),
      criteria_refs: chunk.map((c, i) => ({
        id: i + 1, criterion_key: c.criterion_key, criterion_text: c.criterion_text, depends_on: c.depends_on,
      })),
    });
  }
  return requests;
}

// Stratified batching: round j holds the j-th cohort of every criterion, so no request asks
// about two bands of the same criterion. Listing adjacent bands together invites the model to
// draw a line between them instead of judging each band on its own (docs/EXPERIMENTS.md).
function stratify(sorted) {
  const byKey = new Map();
  for (const c of sorted) {
    if (!byKey.has(c.criterion_key)) byKey.set(c.criterion_key, []);
    byKey.get(c.criterion_key).push(c);
  }
  const rounds = [];
  for (const list of byKey.values()) {
    list.forEach((c, j) => {
      rounds[j] = rounds[j] ?? [];
      rounds[j].push(c);
    });
  }
  return rounds;
}

// ---- request size (K) per model -------------------------------------------------------------
// K is a property of the model and the account, not a constant (DECISIONS AD29):
//   * llm_batch_k caps it: the most cohorts per call validated for accuracy (25 for this prompt
//     on gemini-3.5-flash-lite: no false drops; packing borderline bands broke judgement).
//   * One request at a time (llm_concurrency 1), a batch costs (requests x fixed latency) +
//     (output tokens x per-token latency), so the fewest requests win: K = the cap.
//   * With parallel slots and a model whose latency grows with output, smaller requests run side
//     by side and the last one finishes sooner. The planner then picks the K whose balanced
//     requests, packed onto the slots, finish first, preferring fewer requests on a tie.
// The latency comes from the model's own call log (llm_latency: duration fitted to output
// tokens). A fit that explains little, like Gemini's free tier (R2 0.01: a flat 7-10 s per
// request), counts as a flat cost, and K stays at the cap.
const JUDGE_TOKENS = (n) => 20 + 35 * n; // measured: 25 answers ~ 885 output tokens
const COMPILE_TOKENS = (n) => 125 * n; // measured: 12 criteria ~ 1,400-1,600 tokens

function latencyModel(latency) {
  const usable = latency && Number(latency.calls) >= 20 && Number(latency.r2) >= 0.3 && Number(latency.ms_per_token) > 0;
  return usable ? { l0: Math.max(0, Number(latency.l0_ms) || 0), perToken: Number(latency.ms_per_token) } : null;
}

function sequentialSizes(n, k) {
  const sizes = [];
  for (let left = n; left > 0; left -= k) sizes.push(Math.min(k, left));
  return sizes;
}

function balancedSizes(n, k) {
  const parts = Math.ceil(n / k);
  return Array.from({ length: parts }, (_, i) => Math.floor(n / parts) + (i < n % parts ? 1 : 0));
}

// When the last of these requests finishes on `slots` parallel slots (longest first, each onto
// the least busy slot).
function finishMs(durations, slots) {
  const load = new Array(slots).fill(0);
  for (const d of [...durations].sort((a, b) => b - a)) load[load.indexOf(Math.min(...load))] += d;
  return Math.max(...load);
}

function planBatchSize(cohortCount, criteriaCount, config, latency) {
  const cap = Math.max(1, Number(config.llm_batch_k) || 25);
  const slots = Math.max(1, Math.floor(Number(config.llm_concurrency) || 1));
  const model = latencyModel(latency);
  if (slots === 1 || !model || cohortCount <= 1) return { k: cap, balanced: false };
  const ms = (tokens) => model.l0 + model.perToken * tokens;
  const compile = sequentialSizes(criteriaCount, COMPILE_MAX_CRITERIA).map((n) => ms(COMPILE_TOKENS(n)));
  let best = null;
  for (let k = Math.min(cap, cohortCount); k >= 1; k -= 1) {
    const durations = [...balancedSizes(cohortCount, k).map((n) => ms(JUDGE_TOKENS(n))), ...compile];
    const finish = finishMs(durations, slots);
    if (!best || finish < best.finish - 1) best = { k, finish };
  }
  return { k: best.k, balanced: true };
}

function buildRequests(cohorts, config, plan = { k: Math.max(1, Number(config.llm_batch_k)), balanced: false }) {
  const k = plan.k;
  const sorted = [...cohorts].sort(
    (a, b) => a.criterion_text.localeCompare(b.criterion_text) || a.signature.localeCompare(b.signature),
  );
  const groups = config.llm_batch_mode === 'stratified' ? stratify(sorted) : [sorted];
  const chunks = [];
  for (const group of groups) {
    const sizes = plan.balanced ? balancedSizes(group.length, k) : sequentialSizes(group.length, k);
    let start = 0;
    for (const size of sizes) {
      chunks.push(group.slice(start, start + size));
      start += size;
    }
  }
  const requests = [];
  for (const chunk of chunks) {
    const lines = ['Judge every question. Answer each question id exactly once.', ''];
    const refs = [];
    let current = null;
    let letter = 0;
    chunk.forEach((cohort, i) => {
      if (cohort.criterion_key !== current) {
        current = cohort.criterion_key;
        if (refs.length) lines.push('');
        lines.push(`Criterion ${String.fromCharCode(65 + letter)}: "${cohort.criterion_text}"`);
        letter += 1;
      }
      lines.push(`  ${i + 1}. ${describe(cohort.signature)}`);
      refs.push({ id: i + 1, criterion_key: cohort.criterion_key, signature: cohort.signature });
    });
    requests.push({
      purpose: 'judge_cohorts',
      prompt_version: PROMPT_VERSION,
      model: config.llm_model_match,
      temperature: 0,
      max_output_tokens: 1024 + 96 * chunk.length,
      system: SYSTEM,
      prompt: lines.join('\n'),
      schema: schemaFor(chunk.length),
      cohort_refs: refs,
      planned_k: k,
    });
  }
  return requests;
}

// ---- n8n entry point ----
// The input is the SQL row that found the cohorts; it carries app_config as `config`.
const input = $input.first().json;
const config = input.config;
const cohorts = input.cohorts ?? [];
const criteria = input.criteria ?? [];
const plan = planBatchSize(cohorts.length, criteria.length, config, input.llm_latency);
return [...buildRequests(cohorts, config, plan), ...compileRequests(criteria, config)].map((json) => ({ json }));
