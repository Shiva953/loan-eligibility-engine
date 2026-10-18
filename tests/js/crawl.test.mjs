// Tests for Workflow A's Code nodes, run outside n8n through the same wrapper n8n uses.
// `make test-js` (node --test). The fixtures are the real saved pages.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { runCodeNode, REPO_ROOT } from './harness.mjs';

const CONFIG = {
  crawl_mode: 'fixtures',
  crawl_user_agent: 'LoanEligibilityEngine/0.1 (test)',
  extraction_confidence_min: '0.7',
  llm_model_extract: 'gemini-3.5-flash-lite',
  llm_provider: 'gemini',
  llm_rpd: '500',
  llm_max_retries: '5',
};
const configNode = { config: CONFIG };
const fixture = (slug, file) =>
  readFileSync(path.join(REPO_ROOT, 'tests', 'fixtures', 'crawl', slug, file), 'utf8');

// --- clean_html.js ---------------------------------------------------------------------------

test('cleaner keeps table rows on one line and drops chrome', async () => {
  const html = `<html><head><title>t</title><script>var x=1</script></head><body>
    <nav><ul><li>Home</li></ul></nav>
    <table><tr><td><p>SBI Personal Loan</p></td><td><p>10.00% p.a.</p></td></tr></table>
    <ul><li><a>Car Loan</a></li><li><a>Gold Loan</a></li><li><a>PMAY</a></li><li><a>FD</a></li></ul>
    <p>Minimum income &#8377;25,000 &amp; age 21</p><footer>© 2026</footer></body></html>`;
  const [out] = await runCodeNode('clean_html.js', [{ slug: 's', pages: [{ url: 'https://x/a', body: html }] }]);
  assert.match(out.text, /^SBI Personal Loan \| 10\.00% p\.a\.$/m);
  assert.match(out.text, /Minimum income ₹25,000 & age 21/);
  assert.doesNotMatch(out.text, /var x|Home|© 2026|Car Loan/);
  assert.equal(out.pages, undefined);
});

test('cleaner on the real BankBazaar pages keeps every rate-table row', async () => {
  const pages = ['personal-loan.html', 'personal-loan-eligibility.html'].map((f) => ({
    url: `https://www.bankbazaar.com/${f}`,
    body: fixture('bankbazaar', f),
  }));
  const [out] = await runCodeNode('clean_html.js', [{ slug: 'bankbazaar', pages }]);
  for (const row of ['SBI Personal Loan | 10.00% p.a. to 15.00% p.a.', 'SBI | Up to 76 years | NA & Rs.25,000']) {
    assert.ok(out.text.includes(row), `missing row: ${row}`);
  }
  assert.ok(out.text_chars < out.raw_chars / 10, 'cleaned text should be under 10% of the HTML');
});

test('cleaner reveals HDFC criteria shipped as escaped HTML', async () => {
  const [out] = await runCodeNode('clean_html.js', [
    { slug: 'hdfc-bank', pages: [{ url: 'u', body: fixture('hdfc-bank', 'eligibility-criteria.html') }] },
  ]);
  assert.match(out.text, /Minimum monthly net income of ₹25,000/);
  assert.doesNotMatch(out.text, /<li>|&lt;/);
});

// --- plan_fetches.js (robots.txt) -------------------------------------------------------------

const SOURCE = {
  source_id: 7,
  slug: 'demo',
  page_urls: ['https://example.com/personal-loan.html', 'https://example.com/private/x.html'],
  fixture_files: ['a.html', 'b.html'],
};

async function planWith(robots, status = 200, config = CONFIG) {
  return runCodeNode('plan_fetches.js', [{ statusCode: status, data: robots }], {
    nodes: { 'Loop over sources': SOURCE, 'Load app_config': { config } },
  });
}

test('robots: a disallowed page blocks the source', async () => {
  const [out] = await planWith('User-agent: *\nDisallow: /private/\n');
  assert.equal(out.robots_blocked, true);
  assert.equal(out.run_status, 'robots_blocked');
  assert.match(out.error, /private\/x\.html/);
});

test('robots: our own group overrides *, and the longest rule wins', async () => {
  const robots = 'User-agent: *\nDisallow: /\n\nUser-agent: LoanEligibilityEngine\nDisallow: /private/\nAllow: /private/x.html\n';
  const out = await planWith(robots);
  assert.equal(out.length, 2);
  assert.ok(out.every((p) => p.robots_blocked === false));
  assert.deepEqual(out.map((p) => p.fetch_url), ['http://fixtures/demo/a.html', 'http://fixtures/demo/b.html']);
});

test('robots: wildcards and $ anchors', async () => {
  const [out] = await planWith('User-agent: *\nDisallow: /*.html$\n');
  assert.equal(out.robots_blocked, true);
});

test('robots: 404 allows everything, 503 assumes disallowed, network failure is an error', async () => {
  assert.equal((await planWith('', 404)).length, 2);
  assert.equal((await planWith('', 503))[0].run_status, 'robots_blocked');
  assert.equal((await planWith('', 0))[0].run_status, 'error');
});

test('robots: live mode fetches the real URLs', async () => {
  const out = await planWith('', 404, { ...CONFIG, crawl_mode: 'live' });
  assert.deepEqual(out.map((p) => p.fetch_url), SOURCE.page_urls);
});

test('robots: the saved robots.txt of every enabled source allows its pages', async () => {
  const sources = JSON.parse(readFileSync(path.join(REPO_ROOT, 'data', 'crawl_sources.json'), 'utf8')).sources;
  for (const s of sources.filter((x) => x.enabled)) {
    const out = await runCodeNode('plan_fetches.js', [{ statusCode: 200, data: fixture(s.slug, 'robots.txt') }], {
      nodes: {
        'Loop over sources': { source_id: 1, slug: s.slug, page_urls: s.pages.map((p) => p.url), fixture_files: s.pages.map((p) => p.fixture) },
        'Load app_config': configNode,
      },
    });
    assert.ok(out.every((p) => p.robots_blocked === false), `${s.slug} blocked by its own robots.txt`);
  }
});

// --- collect_pages.js ------------------------------------------------------------------------

test('collect: all 200s produce one source item; any failure produces an error item', async () => {
  const plans = [{ url: 'https://a/1' }, { url: 'https://a/2' }];
  const nodes = { 'Loop over sources': { source_id: 1, slug: 's', display_name: 'S', kind: 'lender' }, 'Plan page fetches': plans };
  const [ok] = await runCodeNode('collect_pages.js', [{ statusCode: 200, data: 'x' }, { statusCode: 200, data: 'y' }], { nodes });
  assert.equal(ok.fetch_ok, true);
  assert.deepEqual(ok.pages.map((p) => p.body), ['x', 'y']);
  const [bad] = await runCodeNode('collect_pages.js', [{ statusCode: 200, data: 'x' }, { statusCode: 403, data: '' }], { nodes });
  assert.equal(bad.fetch_ok, false);
  assert.equal(bad.http_status, 403);
  assert.match(bad.error, /HTTP 403 for https:\/\/a\/2/);
});

// --- build_extraction.js ---------------------------------------------------------------------

test('extraction request carries the page text and a closed schema', async () => {
  const [req] = await runCodeNode('build_extraction.js', [{ display_name: 'HDFC Bank', kind: 'lender', text: 'Age 21 to 60' }], {
    nodes: { 'Load app_config': configNode },
  });
  assert.equal(req.purpose, 'extract_products');
  assert.equal(req.model, 'gemini-3.5-flash-lite');
  assert.match(req.prompt, /Source: HDFC Bank \(lender\)\n\nAge 21 to 60/);
  const item = req.schema.properties.products.items;
  assert.equal(item.additionalProperties, false);
  assert.deepEqual([...item.required].sort(), Object.keys(item.properties).sort());
  assert.deepEqual(item.properties.employment_allowed.items.enum, ['Salaried', 'Self-Employed', 'Business']);
});

// --- validate_products.js: stated credit-score minimums, read from the real pages ------------

const modelSaid = (lender, score, kind) => ({
  lender, product_name: `${lender} Personal Loan`, interest_rate_min_pct: 10.5, interest_rate_max_pct: null,
  min_age: 21, max_age: 60, min_credit_score: score, credit_score_kind: kind, income_thresholds: [],
  employment_allowed: ['Salaried'], other_criteria: [], evidence: 'e',
  confidence: { interest_rate: 1, age: 1, credit_score: 0.5, income: 1, employment: 1 },
});

async function validateOn(slug, files, kind, product) {
  const pages = files.map((f) => ({ url: `https://x/${f}`, body: fixture(slug, f) }));
  const [cleaned] = await runCodeNode('clean_html.js', [{ slug, pages }]);
  const [out] = await runCodeNode('validate_products.js', [{ ok: true, data: { products: [product] }, calls: 1, tokens_in: 1, tokens_out: 1 }], {
    nodes: {
      'Loop over sources': { source_id: 1, page_urls: ['https://x/a'], kind },
      'Hash page text': { content_hash: 'h', text: cleaned.text },
      'Load app_config': configNode,
    },
  });
  return out;
}

test("HDFC's page says a minimum of 720: it filters, even when the model called it preferred", async () => {
  const out = await validateOn('hdfc-bank', ['eligibility-criteria.html'], 'lender', modelSaid('HDFC Bank', 720, 'preferred'));
  const [p] = out.products;
  assert.equal(p.min_credit_score, 720);
  assert.equal(p.field_confidence.min_credit_score, 1); // read by code, so the S6 guard keeps it
  assert.ok(out.issues.some((i) => i.includes('minimum credit score of 720')));
});

test("Kotak's stated 685 is the minimum; its \"750 is generally good\" stays a preference", async () => {
  const out = await validateOn('kotak', ['eligibility.html', 'interest-rate.html'], 'lender', modelSaid('Kotak Mahindra Bank', 750, 'preferred'));
  const [p] = out.products;
  assert.equal(p.min_credit_score, 685);
  assert.ok(p.soft_criteria.some((c) => c.origin === 'preferred_score' && c.rule.value === 750));
});

test("an aggregator's general score requirement (750) applies to listed products that state none", async () => {
  const pages = ['personal-loan.html', 'personal-loan-eligibility.html'];
  const own = await validateOn('bankbazaar', pages, 'aggregator', modelSaid('SBI', null, 'not_stated'));
  assert.equal(own.products[0].min_credit_score, 750); // not the FAQ's "650 to consider"
  assert.ok(own.issues.some((i) => i.includes("general requirement, 750")));
  const sure = { ...modelSaid('Axis', 700, 'minimum'), confidence: { interest_rate: 1, age: 1, credit_score: 1, income: 1, employment: 1 } };
  const stated = await validateOn('bankbazaar', pages, 'aggregator', sure);
  assert.equal(stated.products[0].min_credit_score, 700);
  const bare = { ...modelSaid('Union Bank of India', null, 'not_stated'), min_age: null, max_age: null, employment_allowed: [] };
  const none = await validateOn('bankbazaar', pages, 'aggregator', bare);
  assert.equal(none.products[0].min_credit_score, null); // nothing of its own: still matches nobody
});
