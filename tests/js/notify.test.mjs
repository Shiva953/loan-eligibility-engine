// Tests for Workflow C's Code nodes: settings guard, rendering, the recipient check, SES results.
import test from 'node:test';
import assert from 'node:assert/strict';
import { runCodeNode } from './harness.mjs';

const ALIASES = ['shivaset2+1@gmail.com', 'shivaset2+2@gmail.com'];
const CONFIG = {
  email_mode: 'test_aliases', email_aliases: ALIASES.join(','), email_cap: '2',
  ses_sender: 'shivaset2@gmail.com', ses_max_per_second: '1',
};
const SETTINGS = { mode: 'test_aliases', aliases: ALIASES, cap: 2, sender: 'shivaset2@gmail.com', pause_seconds: 1 };

// --- email_settings.js -------------------------------------------------------------------------

async function settings(config, configError = null) {
  const [out] = await runCodeNode('email_settings.js', [{ config, config_error: configError, emails: [] }]);
  return out;
}

test('valid email switches pass through, with the pause between sends', async () => {
  assert.deepEqual(await settings(CONFIG), SETTINGS);
  assert.equal((await settings({ ...CONFIG, ses_max_per_second: '14' })).pause_seconds, 0.072);
});

test('anything but an explicit safe setting refuses to run', async () => {
  await assert.rejects(settings({ ...CONFIG, email_mode: 'live' }), /refusing to run/);
  await assert.rejects(settings({ ...CONFIG, email_mode: undefined }), /refusing to run/);
  await assert.rejects(settings({ ...CONFIG, email_cap: '3' }), /between 1 and 2/);
  await assert.rejects(settings({ ...CONFIG, email_cap: '0' }), /between 1 and 2/);
  await assert.rejects(settings({ ...CONFIG, email_aliases: 'a@x.com,A@x.com' }), /twice/);
  await assert.rejects(settings({ ...CONFIG, email_aliases: 'not-an-address' }), /comma-separated/);
  await assert.rejects(settings({ ...CONFIG, ses_sender: '' }), /SES_SENDER/);
  // The planning query's own check is a backstop: its reason stops the workflow too.
  await assert.rejects(settings(CONFIG, 'EMAIL_CAP must be between 1 and 2'), /refusing to run/);
});

// --- render_emails.js --------------------------------------------------------------------------

const PRODUCTS = [
  { lender: 'SBI', product: 'SBI Personal Loan', rate_min: 10, rate_max: 15, status: 'matched', caveats: [],
    conditions: ['Minimum 1 year in current job', 'Resident of a metro city', 'Salary account with SBI'] },
  { lender: 'Kotak <Mahindra>', product: 'Personal Loan & more', rate_min: 10.99, rate_max: null, status: 'llm_matched',
    caveats: ['May not apply: Applicants must be salaried at MNCs'], conditions: [] },
  { lender: 'HDFC Bank', product: 'HDFC Personal Loan', rate_min: null, rate_max: null, status: 'matched', caveats: [], conditions: [] },
];

async function render(emails, settingsOverride = SETTINGS) {
  return runCodeNode('render_emails.js', [settingsOverride], {
    nodes: {
      'Plan notifications': { batch_id: 'b', email_mode: settingsOverride.mode, emails },
      'Check email settings': settingsOverride,
    },
  });
}

test('a pending email is ready to send to its alias, with both sections and escaped text', async () => {
  const [email] = await render([{ notification_id: 7, user_id: 'u1', status: 'pending', recipient: ALIASES[0],
    rn: 1, name: 'Ravi <b>Kumar</b>', products: PRODUCTS }]);
  assert.equal(email.send, true);
  assert.equal(email.to, ALIASES[0]);
  assert.equal(email.from, 'shivaset2@gmail.com');
  assert.equal(email.subject, '[Test] Ravi, 3 personal loans you may qualify for');
  assert.equal(email.products, 3);
  assert.ok(email.html.includes('Kotak &lt;Mahindra&gt;'));
  assert.ok(email.html.includes('Personal Loan &amp; more'));
  assert.ok(!email.html.includes('<b>Kumar'));
  assert.ok(email.html.includes('You meet the published criteria'));
  assert.ok(email.html.includes('You may also qualify'));
  assert.ok(email.text.includes('- SBI: SBI Personal Loan (10%–15% a year)'));
  assert.ok(email.text.includes('Check first: Applicants must be salaried at MNCs'));
  assert.ok(email.text.includes('The lender also asks for: Minimum 1 year in current job; Resident of a metro city (+1 more)'));
  assert.ok(email.text.includes('(from 10.99% a year)'));
  assert.ok(email.text.includes('(rate not published)'));
  assert.ok(email.text.includes('not to the address in the uploaded file'));
});

test('suppressed mode renders previews that are never sent', async () => {
  const previews = await render(
    [{ notification_id: 8, user_id: 'u2', status: 'suppressed', recipient: null, rn: 1, name: 'Asha', products: PRODUCTS.slice(0, 1) }],
    { ...SETTINGS, mode: 'suppressed' },
  );
  assert.equal(previews.length, 1);
  assert.equal(previews[0].send, false);
  assert.equal(previews[0].to, null);
  assert.equal(previews[0].subject, '[Test] Asha, 1 personal loan you may qualify for');
});

test('nobody to email gives one "none" item, so the workflow still finishes', async () => {
  assert.deepEqual(await render([]), [{ none: true, send: false }]);
});

// --- recipient_check.js ------------------------------------------------------------------------

function email(to, extra = {}) {
  return { notification_id: 1, send: true, to, from: 'shivaset2@gmail.com', subject: 's', html: 'h', ...extra };
}

async function check(emails, settingsOverride = SETTINGS) {
  return runCodeNode('recipient_check.js', emails, { nodes: { 'Check email settings': settingsOverride } });
}

test('emails to the aliases pass the final check unchanged', async () => {
  const emails = [email(ALIASES[0]), email(ALIASES[1])];
  assert.deepEqual(await check(emails), emails);
});

test('the final check refuses anything outside the aliases, the cap or the sender', async () => {
  await assert.rejects(check([email('someone@example.com')]), /not one of EMAIL_ALIASES/);
  await assert.rejects(check([email(ALIASES[0]), email(ALIASES[0])]), /two emails/);
  await assert.rejects(check([email(ALIASES[0]), email(ALIASES[1]), email(ALIASES[0])]), /EMAIL_CAP is 2/);
  await assert.rejects(check([email(ALIASES[0], { from: 'admin@example.com' })]), /SES_SENDER/);
  await assert.rejects(check([email(ALIASES[0], { send: false })]), /not one of EMAIL_ALIASES/);
  await assert.rejects(check([email(ALIASES[0])], { ...SETTINGS, mode: 'suppressed' }), /not test_aliases/);
});

// --- ses_result.js -----------------------------------------------------------------------------

async function result(response) {
  const [out] = await runCodeNode('ses_result.js', [response], {
    nodes: { 'Loop over emails': { notification_id: 42 } },
  });
  return out;
}

test('an SES message id means sent; an error or no id means failed', async () => {
  assert.deepEqual(await result({ SendEmailResponse: { SendEmailResult: { MessageId: 'abc' } } }),
    { notification_id: 42, outcome: 'sent', ses_message_id: 'abc', error: '' });
  assert.deepEqual(await result({ message: 'Email address is not verified', error: { code: 400 } }),
    { notification_id: 42, outcome: 'failed', ses_message_id: '', error: 'Email address is not verified' });
  assert.deepEqual(await result({}),
    { notification_id: 42, outcome: 'failed', ses_message_id: '', error: 'SES returned no message id' });
});

test('a long list stays under the SES URL budget: best rates first, the rest summed up', async () => {
  const many = Array.from({ length: 26 }, (_, i) => ({
    lender: `Lender number ${i}`, product: `A personal loan with a long descriptive name ${i}`,
    rate_min: 30 - i, rate_max: 40, status: i % 3 ? 'matched' : 'llm_matched',
    caveats: i % 3 ? [] : ['May not apply: Applicants must be salaried at large private employers'],
    conditions: ['Minimum 2 years of total work experience', 'Resident of a listed city', 'Salary account'],
  }));
  const [email] = await render([{ notification_id: 9, user_id: 'u3', status: 'pending', recipient: ALIASES[0],
    rn: 1, name: 'Saanvi', products: many }]);
  assert.ok(encodeURIComponent(email.html).length <= 7000, `encoded ${encodeURIComponent(email.html).length}`);
  assert.ok(email.products_shown < 26 && email.products_shown > 3);
  assert.ok(email.html.includes(`${26 - email.products_shown} more products you may qualify for`));
  assert.ok(email.html.indexOf('Lender number 25') < email.html.indexOf('Lender number 22')); // 5% before 8%
  assert.equal(email.subject, '[Test] Saanvi, 26 personal loans you may qualify for');
  assert.ok(email.text.includes('Lender number 0')); // the text version keeps every product
});
