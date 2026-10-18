// Code node "Collect verdicts" (Workflow B). Mode: Run Once for All Items.
//
// S4 + S5: turns the gateway's answers back into cache rows. Cohort verdicts are keyed by
// (criterion_key, signature), so one write fans each verdict out to every user in the cohort.
// Compiled rules are keyed by criterion_key. An answer that is missing, duplicated or invalid
// is not cached, so its pairs stay pending_llm (or its criterion uncompiled) and the next run
// asks again. Nothing is guessed: a malformed clause is dropped, never repaired.
//
// Input:  one item per request: { cohort_refs: [{id, criterion_key, signature}] or
//         criteria_refs: [{id, criterion_key, criterion_text, depends_on}], ok, data, calls,
//         tokens_in, tokens_out, model, prompt_version, error }
// Output: one item { decisions: [...], rules: [...], calls, tokens_in, tokens_out, requests,
//         failed_requests, unanswered }

const VERDICTS = new Set(['eligible', 'ineligible', 'uncertain']);
const CLAUSE_VERDICTS = new Set(['eligible', 'ineligible']);
const EMPLOYMENT = new Set(['Salaried', 'Self-Employed', 'Business']);
const BOUNDS = ['age_min', 'age_max', 'credit_score_min', 'credit_score_max', 'monthly_income_min', 'monthly_income_max'];

// A clause the SQL can apply, or null. Bounds must be finite numbers or null.
function cleanClause(c) {
  if (!c || !CLAUSE_VERDICTS.has(c.verdict)) return null;
  const employment = Array.isArray(c.employment_status) ? c.employment_status : [];
  if (!employment.every((e) => EMPLOYMENT.has(e))) return null;
  const out = { verdict: c.verdict, employment_status: [...new Set(employment)] };
  for (const b of BOUNDS) {
    const v = c[b] ?? null;
    if (v !== null && !Number.isFinite(v)) return null;
    out[b] = v;
  }
  return out;
}

function collectRules(r, rules) {
  const refs = r.criteria_refs ?? [];
  const byId = new Map();
  for (const rule of r.data?.rules ?? []) {
    if (Number.isInteger(rule.id) && Array.isArray(rule.clauses) && !byId.has(rule.id)) byId.set(rule.id, rule);
  }
  let unanswered = 0;
  for (const ref of refs) {
    const rule = byId.get(ref.id);
    if (!rule) {
      unanswered += 1;
      continue;
    }
    rules.push({
      criterion_key: ref.criterion_key,
      criterion_text: ref.criterion_text,
      depends_on: ref.depends_on,
      clauses: rule.clauses.map(cleanClause).filter(Boolean),
      note: String(rule.note ?? '').slice(0, 300),
      model: r.model,
      prompt_version: r.prompt_version,
    });
  }
  return unanswered;
}

function collect(results) {
  const decisions = [];
  const rules = [];
  let calls = 0;
  let tokensIn = 0;
  let tokensOut = 0;
  let failed = 0;
  let unanswered = 0;
  for (const r of results) {
    calls += r.calls ?? 0;
    tokensIn += r.tokens_in ?? 0;
    tokensOut += r.tokens_out ?? 0;
    const refs = r.cohort_refs ?? [];
    if (!r.ok) {
      failed += 1;
      unanswered += refs.length + (r.criteria_refs ?? []).length;
      continue;
    }
    if (r.criteria_refs) {
      unanswered += collectRules(r, rules);
      continue;
    }
    const byId = new Map();
    for (const v of r.data?.verdicts ?? []) {
      if (Number.isInteger(v.id) && VERDICTS.has(v.verdict) && !byId.has(v.id)) byId.set(v.id, v);
    }
    for (const ref of refs) {
      const v = byId.get(ref.id);
      if (!v) {
        unanswered += 1;
        continue;
      }
      decisions.push({
        criterion_key: ref.criterion_key,
        signature: ref.signature,
        verdict: v.verdict,
        reason: String(v.reason ?? '').slice(0, 300),
        model: r.model,
        prompt_version: r.prompt_version,
      });
    }
  }
  return {
    decisions,
    rules,
    calls,
    tokens_in: tokensIn,
    tokens_out: tokensOut,
    requests: results.length,
    failed_requests: failed,
    unanswered,
  };
}

// ---- n8n entry point ----
return [{ json: collect($input.all().map((item) => item.json)) }];
