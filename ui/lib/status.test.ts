import assert from 'node:assert/strict';
import { test } from 'node:test';
import { emailLine, explain, funnel, steps, summary, type Status, type UploadProgress } from './status.ts';

const UPLOADED: UploadProgress = { done: true, detail: '1.9 MB sent', ms: 420 };
const states = (s: Status | null, upload: UploadProgress = UPLOADED) => steps(upload, s).map((step) => step.state);

const notified: Status = {
  status: 'notified', done: true, error: null, email_mode: 'suppressed',
  rows: { total: 2000, valid: 1905, rejected: 95 },
  matching: { pairs: 40515, by_status: { matched: 39297, llm_matched: 1218 }, llm_calls: 0, customers_with_offers: 1905, products: 29 },
  emails: { suppressed: 1905, to_send: 0 },
  ms: { ingest: 81, wait_for_match: 375, match: 197, notify: 41, total: 693 },
};

test('while uploading, only the upload step is active', () => {
  assert.deepEqual(states(null, { done: false, detail: '42%', ms: null }), ['active', 'waiting', 'waiting', 'waiting']);
});

test('each batch status lights the right step', () => {
  assert.deepEqual(states({ status: 'uploaded', done: false }), ['done', 'active', 'waiting', 'waiting']);
  assert.deepEqual(states({ status: 'ingested', done: false }), ['done', 'done', 'active', 'waiting']);
  assert.deepEqual(states({ status: 'matching', done: false }), ['done', 'done', 'active', 'waiting']);
  assert.deepEqual(states({ status: 'notifying', done: false }), ['done', 'done', 'done', 'active']);
  assert.deepEqual(states(notified), ['done', 'done', 'done', 'done']);
});

test('a finished batch reads as numbers a person cares about', () => {
  const [, check, match, notify] = steps(UPLOADED, notified);
  assert.equal(check?.detail, '1,905 customers ready · 95 rows skipped');
  assert.equal(match?.detail, "1,905 meet a lender's criteria · 40,515 loan matches across 29 products");
  assert.equal(match?.ms, 375 + 197);
  assert.equal(notify?.detail, '1,905 emails ready, sending is switched off');
  assert.deepEqual(summary(notified), { customers: 1905, skipped: 95, withOffers: 1905, matches: 40515, products: 29 });
});

test('Indian digit grouping above a lakh', () => {
  const big: Status = { ...notified, rows: { total: 100000, valid: 98255, rejected: 1745 } };
  assert.equal(steps(UPLOADED, big)[1]?.detail, '98,255 customers ready · 1,745 rows skipped');
  assert.equal(emailLine({ ...notified, emails: { sent: 2, suppressed: 198253 } }), '2 emails sent · 1,98,253 held back in test mode');
});

test('a file refused by ingest fails the check step with a fix', () => {
  const failed: Status = { status: 'failed', done: true, error: 'missing column(s): email', ms: {} };
  const [, check, match] = steps(UPLOADED, failed);
  assert.equal(check?.state, 'failed');
  assert.equal(check?.detail, 'The file has no email column. Add it to the first row and upload again.');
  assert.equal(match?.state, 'waiting');
});

test('a failure after matching marks the earlier steps done', () => {
  const failed: Status = { ...notified, status: 'failed', error: 'SES throttled', ms: { ingest: 80, match: 200 } };
  assert.deepEqual(states(failed), ['done', 'done', 'done', 'failed']);
  assert.equal(explain(null).startsWith('Something went wrong'), true);
});

test('no offers and test-mode sends are described plainly', () => {
  assert.equal(emailLine({ ...notified, emails: {} }), 'No customer had an offer to send');
  assert.equal(emailLine({ ...notified, email_mode: 'test_aliases', emails: { suppressed: 5 } }), '5 held back in test mode');
});

test('the funnel goes from every pair to the AI calls this upload made', () => {
  const stages = funnel(notified);
  assert.deepEqual(stages?.map((s) => s.value), [1905 * 29, 40515, 1218, 0]);
  assert.match(stages?.[0]?.note ?? '', /1,905 customers × 29 products/);
  assert.match(stages?.[3]?.note ?? '', /cached/);
  assert.equal(funnel({ status: 'ingested', done: false }), null);
});
