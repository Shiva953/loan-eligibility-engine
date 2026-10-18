// Deterministic matching done in JavaScript, the way an n8n Code node would do it.
//
//   d0  naive: nested loop over users × products, one {json} item per passing pair. This is
//       what "implement the matching in n8n" turns into if the data is pulled into the workflow.
//   d4  bitmask: each field's thresholds cut it into intervals; for every (field, employment,
//       interval) the set of products whose tests pass is precomputed as a bitmask (M ≤ 32 per
//       word). A user's eligible products are the AND of three mask lookups (found by binary
//       search) and an employment mask: O(log c) per field per user instead of O(M), then one
//       pass over the set bits to emit pairs.
//
// Usage: node bench/variants/js_matchers.mjs <d0|d4> <input.json> <pairs-out.txt>
// Input:  { users: [{user_id, monthly_income, credit_score, employment_status, age}],
//           products: [{product_id, min_monthly_income, ..., allowed_employment, filter_rules, has_fuzzy}] }
// Output: one "user_id,product_id" line per pair; timing and heap figures as JSON on stdout.
import { readFileSync, writeFileSync } from 'node:fs';

const FIELDS = ['monthly_income', 'credit_score', 'age'];
const EMPLOYMENT = ['Salaried', 'Self-Employed', 'Business'];

function passes(u, p) {
  if (p.min_monthly_income !== null && u.monthly_income < p.min_monthly_income) return false;
  if (p.min_credit_score !== null && u.credit_score < p.min_credit_score) return false;
  if (p.min_age !== null && u.age < p.min_age) return false;
  if (p.max_age !== null && u.age > p.max_age) return false;
  if (p.allowed_employment !== null && !p.allowed_employment.includes(u.employment_status)) return false;
  for (const [field, op, value, when] of p.filter_rules) {
    if (when.length && !when.includes(u.employment_status)) continue;
    if (op === '>=' ? u[field] < value : u[field] > value) return false;
  }
  return true;
}

function d0(users, products) {
  const items = [];
  for (const u of users) {
    for (const p of products) {
      if (passes(u, p)) {
        items.push({ json: { user_id: u.user_id, product_id: p.product_id, status: p.has_fuzzy ? 'pending_llm' : 'matched' } });
      }
    }
  }
  return items.map((i) => [i.json.user_id, i.json.product_id]);
}

// Tests on one field for one product, as a predicate on the value (for a given employment).
function fieldTest(p, field, employment) {
  const tests = [];
  if (field === 'monthly_income' && p.min_monthly_income !== null) tests.push((v) => v >= p.min_monthly_income);
  if (field === 'credit_score' && p.min_credit_score !== null) tests.push((v) => v >= p.min_credit_score);
  if (field === 'age' && p.min_age !== null) tests.push((v) => v >= p.min_age);
  if (field === 'age' && p.max_age !== null) tests.push((v) => v <= p.max_age);
  for (const [f, op, value, when] of p.filter_rules) {
    if (f !== field || (when.length && !when.includes(employment))) continue;
    tests.push(op === '>=' ? (v) => v >= value : (v) => v <= value);
  }
  return (v) => tests.every((t) => t(v));
}

function cutPoints(products, field) {
  const cuts = new Set();
  for (const p of products) {
    if (field === 'monthly_income' && p.min_monthly_income !== null) cuts.add(p.min_monthly_income);
    if (field === 'credit_score' && p.min_credit_score !== null) cuts.add(p.min_credit_score);
    if (field === 'age' && p.min_age !== null) cuts.add(p.min_age);
    if (field === 'age' && p.max_age !== null) cuts.add(p.max_age + 1);
    for (const [f, op, value] of p.filter_rules) if (f === field) cuts.add(op === '>=' ? value : value + 1);
  }
  return [...cuts].sort((a, b) => a - b);
}

// Number of cuts ≤ v: the interval index, by binary search.
function bucket(cuts, v) {
  let lo = 0;
  let hi = cuts.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cuts[mid] <= v) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function d4(users, products) {
  const M = products.length;
  const W = Math.ceil(M / 32) || 1;
  const cuts = Object.fromEntries(FIELDS.map((f) => [f, cutPoints(products, f)]));
  // masks[field][employment] = Uint32Array((cuts+1) * W)
  const masks = {};
  for (const f of FIELDS) {
    masks[f] = {};
    const c = cuts[f];
    for (const e of EMPLOYMENT) {
      const m = new Uint32Array((c.length + 1) * W);
      for (let b = 0; b <= c.length; b += 1) {
        const rep = b === 0 ? (c.length ? c[0] - 1 : 0) : c[b - 1];
        products.forEach((p, i) => {
          if (fieldTest(p, f, e)(rep)) m[b * W + (i >> 5)] |= 1 << (i & 31);
        });
      }
      masks[f][e] = m;
    }
  }
  const empMask = Object.fromEntries(EMPLOYMENT.map((e) => {
    const m = new Uint32Array(W);
    products.forEach((p, i) => {
      if (p.allowed_employment === null || p.allowed_employment.includes(e)) m[i >> 5] |= 1 << (i & 31);
    });
    return [e, m];
  }));
  const ids = products.map((p) => p.product_id);
  const pairs = [];
  for (const u of users) {
    const e = u.employment_status;
    const bi = bucket(cuts.monthly_income, u.monthly_income) * W;
    const bs = bucket(cuts.credit_score, u.credit_score) * W;
    const ba = bucket(cuts.age, u.age) * W;
    for (let w = 0; w < W; w += 1) {
      let x = masks.monthly_income[e][bi + w] & masks.credit_score[e][bs + w] & masks.age[e][ba + w] & empMask[e][w];
      while (x) {
        const low = x & -x;
        pairs.push([u.user_id, ids[w * 32 + (31 - Math.clz32(low))]]);
        x ^= low;
      }
    }
  }
  return pairs;
}

const [variant, input, output] = process.argv.slice(2);
const data = JSON.parse(readFileSync(input, 'utf8'));
globalThis.gc?.();
const heapBefore = process.memoryUsage().heapUsed;
const started = process.hrtime.bigint();
const pairs = (variant === 'd0' ? d0 : d4)(data.users, data.products);
const ms = Number(process.hrtime.bigint() - started) / 1e6;
const heapMb = (process.memoryUsage().heapUsed - heapBefore) / 1048576;
writeFileSync(output, pairs.map((p) => `${p[0]},${p[1]}`).join('\n'));
console.log(JSON.stringify({ variant, users: data.users.length, products: data.products.length, pairs: pairs.length, ms, heap_mb: heapMb }));
