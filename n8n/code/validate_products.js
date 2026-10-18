// Code node "Validate products" (Workflow A). Mode: Run Once for All Items.
//
// Turns the LLM's page-level extraction into the hard-filter columns of loan_products (S1) and
// applies the confidence guard (S6). Everything that decides who is filtered out happens here,
// in code that can be tested, never in the model:
//   * Ranges: rates must be in (0, 60] % p.a., ages in 18-100, scores in 300-900. Swapped
//     min/max values are put back in order.
//   * Income: annual amounts become monthly (divide by 12). The lowest threshold for anyone
//     becomes the SQL filter, so nobody is wrongly excluded. Stricter employment-specific
//     thresholds become `compilable` rules. Thresholds that depend on something we don't know
//     (salary account, employer) become `unknown_field` caveats.
//   * Credit score: only a stated minimum filters. A "preferred" score becomes a `compilable`
//     ranking rule. On a lender's own page, code also reads the text: a sentence that states a
//     score as a requirement ("you need a minimum credit score of 720", "CIBIL Score: 685 or
//     higher") sets the minimum, even if the model called it preferred. The lowest stated
//     requirement wins. On an aggregator page the stated requirement is the source's general
//     one ("Credit Score | CIBIL score of 750 or more"); it applies to each listed product that
//     states criteria of its own but no score. A sentence about what lenders will "consider" is
//     not an eligibility bar and is skipped.
//   * Income tiers reserved for a relationship we can't verify (the bank's own employees, its
//     salary-account holders, existing customers) don't set the floor; they stay caveats. The
//     floor is the general public's tier.
//   * S6: a hard-filter field whose confidence is below extraction_confidence_min is not used
//     as a filter (stored as NULL). It becomes a `fuzzy` criterion instead, so that product's
//     pairs go to the LLM with the stated value rather than being decided by an untrusted
//     rule.
//   * criteria_hash covers the decision-relevant criteria only (no confidences, no evidence),
//     so it changes exactly when matching could change.

const crypto = require('crypto');

const EMPLOYMENT = ['Salaried', 'Self-Employed', 'Business'];
const FACTS = ['monthly_income', 'credit_score', 'employment_status', 'age'];
const CLASSES = ['compilable', 'fuzzy', 'unknown_field'];
const MAX_MONTHLY_INCOME = 10000000;
const SUSPICIOUS_MONTHLY = 500000;

function num(value) {
  const n = typeof value === 'string' ? Number(value.replace(/,/g, '')) : value;
  return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

function text(value) {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

function rupees(n) {
  return `₹${Math.round(n).toLocaleString('en-IN')}`;
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((k) => [k, canonical(value[k])]));
  }
  return value;
}

function sha256(value) {
  return crypto.createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex');
}

function rates(raw, issues) {
  let min = num(raw.interest_rate_min_pct);
  let max = num(raw.interest_rate_max_pct);
  for (const [name, value] of [['min', min], ['max', max]]) {
    if (value !== null && !(value > 0 && value <= 60)) {
      issues.push(`interest rate ${name} ${value}% is outside (0, 60]: dropped`);
      if (name === 'min') min = null;
      else max = null;
    }
  }
  if (min !== null && max !== null && min > max) {
    issues.push(`interest rate min ${min} > max ${max}: swapped`);
    [min, max] = [max, min];
  }
  return { interest_rate_min: min, interest_rate_max: max };
}

function ages(raw, issues) {
  let min = num(raw.min_age);
  let max = num(raw.max_age);
  if (min !== null && !(min >= 18 && min <= 100)) { issues.push(`min age ${min} out of range: dropped`); min = null; }
  if (max !== null && !(max >= 18 && max <= 100)) { issues.push(`max age ${max} out of range: dropped`); max = null; }
  if (min !== null && max !== null && min > max) {
    issues.push(`min age ${min} > max age ${max}: both dropped`);
    return { min_age: null, max_age: null };
  }
  return { min_age: min, max_age: max };
}

const SCORE_MENTION = /\b(?:credit|cibil)\s+score\b/i;
const SCORE_REQUIRED = /\b(?:minimum|at least|need|needs|required|requires|must|eligible)\b|\bor (?:higher|more|above)\b|\band above\b/i;
const SCORE_PREFERRED = /\b(?:good|ideal|prefer|preferred|generally|recommended|better|improves?|likely|chances|typically|consider|considered)\b/i;

/** The lowest credit score a page states as a requirement, or null: the text, read by code. */
function statedMinimumScore(pageText) {
  let lowest = null;
  for (const sentence of String(pageText || '').split(/(?<=[.?!])\s+|\n+|\s\|\s/)) {
    if (!SCORE_MENTION.test(sentence) || !SCORE_REQUIRED.test(sentence) || SCORE_PREFERRED.test(sentence)) continue;
    for (const match of sentence.matchAll(/\b([3-9]\d{2})\b/g)) {
      const value = Number(match[1]);
      if (value >= 300 && value <= 900 && (lowest === null || value < lowest)) lowest = value;
    }
  }
  return lowest;
}

function creditScore(raw, issues, soft, pageMinimum) {
  const score = num(raw.min_credit_score);
  let minimum = null;
  if (score !== null && raw.credit_score_kind !== 'not_stated') {
    if (!(score >= 300 && score <= 900)) {
      issues.push(`credit score ${score} out of 300-900: dropped`);
    } else if (raw.credit_score_kind === 'preferred') {
      soft.push({
        text: `Credit score ${score}+ preferred`,
        class: 'compilable',
        depends_on: ['credit_score'],
        origin: 'preferred_score',
        rule: { field: 'credit_score', op: '>=', value: score, effect: 'rank' },
      });
    } else {
      minimum = Math.round(score);
    }
  }
  if (pageMinimum !== null && (minimum === null || pageMinimum < minimum)) {
    issues.push(`the page states a minimum credit score of ${pageMinimum}: used as the filter`);
    return { min_credit_score: pageMinimum, score_from_page: true };
  }
  return { min_credit_score: minimum };
}

const CONCESSION = /\b(?:employees?|staff|salary account|account holders?|corp(?:orate)? salary|existing customers?|pre-?approved)\b/i;
const isConcession = (condition) => Boolean(condition) && CONCESSION.test(condition) && !/\bnon[- ]/i.test(condition);

function income(raw, issues, soft, confidence) {
  const thresholds = [];
  for (const t of Array.isArray(raw.income_thresholds) ? raw.income_thresholds : []) {
    const amount = num(t.amount);
    if (amount === null || amount <= 0) continue;
    const monthly = t.period === 'annual' ? Math.round(amount / 12) : amount;
    if (monthly > MAX_MONTHLY_INCOME) {
      issues.push(`income ${rupees(monthly)}/month is implausible: dropped`);
      continue;
    }
    if (t.period !== 'annual' && monthly >= SUSPICIOUS_MONTHLY) {
      issues.push(`monthly income ${rupees(monthly)} looks annual: confidence lowered`);
      confidence.min_monthly_income = Math.min(confidence.min_monthly_income, 0.4);
    }
    const employment = [...new Set((t.employment || []).filter((e) => EMPLOYMENT.includes(e)))];
    thresholds.push({ monthly, employment, condition: text(t.condition) || null });
  }
  if (!thresholds.length) return { min_monthly_income: null };

  const general = thresholds.filter((t) => !isConcession(t.condition));
  const floor = Math.min(...(general.length ? general : thresholds).map((t) => t.monthly));
  for (const t of thresholds) {
    if (t.condition) {
      soft.push({
        text: `Minimum income ${rupees(t.monthly)}/month ${t.condition}`.replace(/\s+/g, ' '),
        class: 'unknown_field',
        depends_on: [],
        origin: 'income_tier',
      });
    } else if (t.monthly > floor && t.employment.length && t.employment.length < EMPLOYMENT.length) {
      soft.push({
        text: `Minimum income ${rupees(t.monthly)}/month for ${t.employment.join(' / ')}`,
        class: 'compilable',
        depends_on: ['monthly_income', 'employment_status'],
        origin: 'income_tier',
        rule: { field: 'monthly_income', op: '>=', value: t.monthly, when: { employment_status: t.employment } },
      });
    } else if (t.monthly > floor) {
      issues.push(`conflicting unconditional income thresholds; using the lowest, ${rupees(floor)}`);
      confidence.min_monthly_income = Math.min(confidence.min_monthly_income, 0.5);
    }
  }
  return { min_monthly_income: floor };
}

function employmentAllowed(raw) {
  const allowed = [...new Set((raw.employment_allowed || []).filter((e) => EMPLOYMENT.includes(e)))];
  if (!allowed.length || allowed.length === EMPLOYMENT.length) return { allowed_employment: null };
  return { allowed_employment: EMPLOYMENT.filter((e) => allowed.includes(e)) };
}

function otherCriteria(raw, issues, soft) {
  for (const c of Array.isArray(raw.other_criteria) ? raw.other_criteria : []) {
    const body = text(c.text);
    if (!body) continue;
    let cls = CLASSES.includes(c.class) ? c.class : 'unknown_field';
    let dependsOn = [...new Set((c.depends_on || []).filter((f) => FACTS.includes(f)))];
    if (cls === 'unknown_field') dependsOn = [];
    if (cls !== 'unknown_field' && !dependsOn.length) {
      issues.push(`"${body}" is ${cls} but depends on none of our fields: treated as unknown_field`);
      cls = 'unknown_field';
    }
    soft.push({ text: body, class: cls, depends_on: dependsOn, origin: 'page' });
  }
}

// S6: a hard filter the extraction is unsure about is not used as a filter.
const GUARDED = [
  { field: 'min_monthly_income', fact: 'monthly_income', label: 'Minimum monthly income', show: rupees },
  { field: 'min_credit_score', fact: 'credit_score', label: 'Minimum credit score', show: String },
  { field: 'allowed_employment', fact: 'employment_status', label: 'Eligible employment', show: (v) => v.join(', ') },
];

function confidenceGuard(columns, confidence, threshold, issues, soft) {
  for (const { field, fact, label, show } of GUARDED) {
    if (columns[field] !== null && confidence[field] < threshold) {
      soft.push({
        text: `${label} stated as ${show(columns[field])} (extraction confidence ${confidence[field]})`,
        class: 'fuzzy',
        depends_on: [fact],
        origin: 'low_confidence',
      });
      issues.push(`${field} confidence ${confidence[field]} < ${threshold}: not used as a filter`);
      columns[field] = null;
    }
  }
  const ageConfident = confidence.age >= threshold;
  if (!ageConfident && (columns.min_age !== null || columns.max_age !== null)) {
    soft.push({
      text: `Age ${columns.min_age ?? '?'}-${columns.max_age ?? '?'} years (extraction confidence ${confidence.age})`,
      class: 'fuzzy',
      depends_on: ['age'],
      origin: 'low_confidence',
    });
    issues.push(`age confidence ${confidence.age} < ${threshold}: not used as a filter`);
    columns.min_age = null;
    columns.max_age = null;
  }
}

function normalizeProduct(raw, sourceUrl, threshold, pageMinimum = null, generalMinimum = null) {
  const issues = [];
  const lender = text(raw.lender);
  const productName = text(raw.product_name);
  if (!lender || !productName) return { product: null, issues: ['product without lender or name: dropped'] };

  const c = raw.confidence || {};
  const clamp = (v) => Math.round(Math.min(1, Math.max(0, num(v) ?? 0)) * 100) / 100;
  const confidence = {
    interest_rate: clamp(c.interest_rate),
    age: clamp(c.age),
    min_credit_score: clamp(c.credit_score),
    min_monthly_income: clamp(c.income),
    allowed_employment: clamp(c.employment),
  };

  const soft = [];
  const { score_from_page: scoreFromPage, ...score } = creditScore(raw, issues, soft, pageMinimum);
  if (scoreFromPage) confidence.min_credit_score = 1; // read from the page text by code
  const columns = {
    ...rates(raw, issues),
    ...ages(raw, issues),
    ...score,
    ...income(raw, issues, soft, confidence),
    ...employmentAllowed(raw),
  };
  otherCriteria(raw, issues, soft);
  const ownCriteria = ['min_age', 'max_age', 'min_monthly_income'].some((f) => columns[f] !== null)
    || (columns.allowed_employment ?? []).length > 0
    || soft.some((c) => c.class === 'fuzzy' || c.class === 'compilable');
  if (columns.min_credit_score === null && generalMinimum !== null && ownCriteria) {
    columns.min_credit_score = generalMinimum;
    confidence.min_credit_score = 1; // read from the page text by code
    issues.push(`no score of its own: the source's general requirement, ${generalMinimum}, applies`);
  }
  confidenceGuard(columns, confidence, threshold, issues, soft);

  const seen = new Set();
  const softCriteria = soft
    .filter((s) => (seen.has(s.text.toLowerCase()) ? false : seen.add(s.text.toLowerCase())))
    .sort((a, b) => a.class.localeCompare(b.class) || a.text.localeCompare(b.text));
  const criteria = { ...columns, soft_criteria: softCriteria };

  return {
    product: {
      lender,
      product_name: productName,
      source_url: sourceUrl,
      ...columns,
      soft_criteria: softCriteria,
      field_confidence: confidence,
      criteria_json: canonical(criteria),
      criteria_hash: sha256(criteria),
    },
    issues: issues.map((i) => `${productName}: ${i}`),
  };
}

const hardFieldCount = (p) =>
  ['min_age', 'max_age', 'min_credit_score', 'min_monthly_income', 'allowed_employment', 'interest_rate_min']
    .filter((f) => p[f] !== null).length;

// One lender can appear under its full name and its acronym on the same page ("IIFL" and
// "India Infoline Finance Ltd."), or a product under the lender's initials ("PNB Personal
// Loan"). Two entries are the same product when their lenders match by name or acronym (a
// single-word name equal to the other's initials) and their product names match once lender
// words, initials and generic words are removed.
const words = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(' ').filter(Boolean);
const GENERIC = new Set(['bank', 'ltd', 'limited', 'finance', 'financial', 'services', 'powered', 'by']);

function lenderForms(lender) {
  const w = words(lender);
  return {
    core: w.filter((x) => !['bank', 'ltd', 'limited'].includes(x)).join(' '),
    initials: w.map((x) => x[0]).join(''),
    single: w.length === 1 ? w[0] : null,
  };
}

function sameLender(a, b) {
  const x = lenderForms(a);
  const y = lenderForms(b);
  return x.core === y.core || (x.single !== null && x.single === y.initials)
    || (y.single !== null && y.single === x.initials);
}

// The product name without either lender's words or initials, so "PNB Personal Loan" and
// "Punjab National Bank Personal Loan" compare equal.
function productCore(p, otherLender) {
  const drop = new Set([...words(p.lender), ...words(otherLender), lenderForms(p.lender).initials,
                        lenderForms(otherLender).initials, ...GENERIC]);
  return words(p.product_name).filter((x) => !drop.has(x)).join(' ');
}

function validate(extraction, sourceUrl, threshold, pageMinimum = null, generalMinimum = null) {
  const kept = [];
  const issues = [];
  let dropped = 0;
  for (const raw of Array.isArray(extraction?.products) ? extraction.products : []) {
    const { product, issues: productIssues } = normalizeProduct(raw, sourceUrl, threshold, pageMinimum, generalMinimum);
    issues.push(...productIssues);
    if (!product) { dropped += 1; continue; }
    const at = kept.findIndex((k) => sameLender(k.lender, product.lender)
      && productCore(k, product.lender) === productCore(product, k.lender));
    if (at === -1) { kept.push(product); continue; }
    issues.push(`${product.product_name}: same product as ${kept[at].product_name}, kept the more complete entry`);
    dropped += 1;
    if (hardFieldCount(product) > hardFieldCount(kept[at])) kept[at] = product;
  }
  return { products: kept, issues, dropped };
}

// ---- n8n entry point ----
const gateway = $input.first().json;
const source = $('Loop over sources').first().json;
const page = $('Hash page text').first().json;
const config = $('Load app_config').first().json.config;
const threshold = Number(config.extraction_confidence_min);
const stated = statedMinimumScore(page.text);
const pageMinimum = source.kind === 'lender' ? stated : null; // the lender's own minimum
const generalMinimum = source.kind === 'aggregator' ? stated : null; // the source's general one
const { products, issues, dropped } = validate(gateway.data, source.page_urls[0], threshold, pageMinimum, generalMinimum);
return [{
  json: {
    products,
    products_found: products.length,
    products_dropped: dropped,
    issues,
    content_hash: page.content_hash,
    llm_calls: gateway.calls,
    tokens_in: gateway.tokens_in,
    tokens_out: gateway.tokens_out,
  },
}];
