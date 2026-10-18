// Code node "Render emails" (Workflow C). Mode: Run Once for All Items.
//
// One email per planned user (at most EMAIL_CAP): greeted by first name, then the products whose
// published criteria include them, then the ones they "may qualify" for with what to check
// first. In suppressed mode the same emails are rendered as previews and never sent
// (send: false). Everything that came from the CSV or a crawled page is HTML-escaped. The
// recipient is the test alias the planning query chose; no CSV address ever reaches this node.
//
// Input:  the "Plan notifications" row { batch_id, email_mode, emails: [...] } (read by name:
//         this node's own input is the settings check)
// Output: one item per email { notification_id, send, to, from, subject, html, text, products },
//         or a single { none: true, send: false } when there is nobody to email.

const MAX_CONDITIONS = 2;
// n8n's SES node sends the message as query parameters of the request URL (Action=SendEmail),
// and the SES endpoint refuses URLs over about 16 KB with "414 Request-URI Too Large"
// (measured: 9 KB accepted, 18 KB refused; docs/DECISIONS.md V41). So the HTML is kept under
// this budget, measured exactly as the node encodes it, showing the best rates first and
// summing up the rest in one line.
const HTML_BUDGET = 7000;

function escapeHtml(value) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value ?? '').replace(/[&<>"']/g, (c) => map[c]);
}

function percent(v) {
  return `${Number(v).toFixed(2).replace(/\.?0+$/, '')}%`;
}

function rateText(p) {
  if (p.rate_min != null && p.rate_max != null) return `${percent(p.rate_min)}–${percent(p.rate_max)} a year`;
  if (p.rate_min != null) return `from ${percent(p.rate_min)} a year`;
  if (p.rate_max != null) return `up to ${percent(p.rate_max)} a year`;
  return 'rate not published';
}

// What the user should still check: row caveats ("May not apply: ...") and the lender's own
// conditions the data can't verify (work experience, city, ...), at most two of the latter.
function notesFor(p) {
  const notes = [];
  for (const c of p.caveats ?? []) {
    notes.push(c.startsWith('May not apply: ') ? `Check first: ${c.slice('May not apply: '.length)}`
      : c.startsWith('Not checked: ') ? `Not checked yet: ${c.slice('Not checked: '.length)}` : c);
  }
  const conditions = p.conditions ?? [];
  if (conditions.length) {
    const more = conditions.length > MAX_CONDITIONS ? ` (+${conditions.length - MAX_CONDITIONS} more)` : '';
    notes.push(`The lender also asks for: ${conditions.slice(0, MAX_CONDITIONS).join('; ')}${more}`);
  }
  return notes;
}

function firstName(name) {
  return String(name ?? '').trim().split(/\s+/)[0] || 'there';
}

function htmlRow(p) {
  const notes = notesFor(p).map((n) => `<br><span style="color:#5a6476;font-size:13px">${escapeHtml(n)}</span>`).join('');
  return `<tr><td style="padding:10px 28px;border-top:1px solid #eef1f5;font-size:15px;line-height:1.45">`
    + `<strong>${escapeHtml(p.lender)}</strong> · ${escapeHtml(p.product)}`
    + `<br><span style="color:#17202b;font-size:13px">${escapeHtml(rateText(p))}</span>${notes}</td></tr>`;
}

function htmlSection(title, intro, products) {
  if (!products.length) return '';
  return `<tr><td style="padding:18px 28px 6px"><h2 style="margin:0;font-size:16px">${escapeHtml(title)}</h2>`
    + `<p style="margin:4px 0 0;color:#5a6476;font-size:13px">${escapeHtml(intro)}</p></td></tr>`
    + products.map(htmlRow).join('');
}

const FOOTER = 'This is a test email from the Loan Eligibility Engine demo. It was sent to a test inbox, '
  + 'not to the address in the uploaded file. Eligibility is checked against the criteria lenders '
  + 'publish; it is not an offer or an approval of credit.';

const byRate = (a, b) => (a.rate_min ?? Infinity) - (b.rate_min ?? Infinity)
  || String(a.lender).localeCompare(String(b.lender));

function moreRow(hidden) {
  if (!hidden) return '';
  return `<tr><td style="padding:10px 28px;border-top:1px solid #eef1f5;font-size:14px;color:#5a6476">`
    + `…and ${hidden} more product${hidden === 1 ? '' : 's'} you may qualify for.</td></tr>`;
}

function render(email, settings) {
  const first = firstName(email.name);
  const products = email.products ?? [];
  const meets = products.filter((p) => p.status === 'matched').sort(byRate);
  const maybe = products.filter((p) => p.status !== 'matched').sort(byRate);
  const n = products.length;
  const subject = `[Test] ${first}, ${n} personal loan${n === 1 ? '' : 's'} you may qualify for`;
  let shown = n;
  let html = page(first, n, meets, maybe, shown);
  while (shown > 1 && encodeURIComponent(html).length > HTML_BUDGET) {
    shown -= 1;
    html = page(first, n, meets, maybe, shown);
  }
  const textRows = (list) => list.map((p) => [`- ${p.lender}: ${p.product} (${rateText(p)})`,
    ...notesFor(p).map((note) => `    ${note}`)].join('\n'));
  const text = [`Hi ${first},`, '', `${n} personal loan${n === 1 ? '' : 's'} may suit you.`,
    ...(meets.length ? ['', 'You meet the published criteria:', ...textRows(meets)] : []),
    ...(maybe.length ? ['', 'You may also qualify:', ...textRows(maybe)] : []),
    '', FOOTER].join('\n');
  return {
    notification_id: email.notification_id,
    user_id: email.user_id,
    send: settings.mode === 'test_aliases' && email.status === 'pending',
    to: email.recipient ?? null,
    from: settings.sender,
    subject,
    html,
    text,
    products: n,
    products_shown: shown,
  };
}

// The HTML email with the first `shown` products (meeting the criteria first, best rate first).
function page(first, n, meets, maybe, shown) {
  const m = meets.slice(0, shown);
  const y = maybe.slice(0, Math.max(0, shown - m.length));
  return '<!doctype html><html><body style="margin:0;padding:0;background:#f5f7fa;'
    + 'font-family:Arial,Helvetica,sans-serif;color:#17202b">'
    + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa">'
    + '<tr><td align="center" style="padding:24px 12px">'
    + '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;'
    + 'background:#ffffff;border:1px solid #dae0e8;border-radius:8px">'
    + '<tr><td style="padding:24px 28px 4px">'
    + '<p style="margin:0 0 6px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#5a6476">Loan Eligibility Engine</p>'
    + `<h1 style="margin:0 0 10px;font-size:22px;line-height:1.3">Hi ${escapeHtml(first)}, ${n} personal loan${n === 1 ? '' : 's'} may suit you</h1>`
    + '<p style="margin:0;font-size:15px;line-height:1.5">We compared the details from your recent application '
    + 'with the eligibility rules lenders publish. These products include people with your profile.</p>'
    + '</td></tr>'
    + htmlSection('You meet the published criteria', 'Income, credit score, age and employment all fit.', m)
    + htmlSection('You may also qualify', 'Part of the criteria can’t be checked from your details; see the note under each.', y)
    + moreRow(n - m.length - y.length)
    + `<tr><td style="padding:20px 28px 24px;color:#5a6476;font-size:12px;line-height:1.5;border-top:1px solid #eef1f5">${escapeHtml(FOOTER)}</td></tr>`
    + '</table></td></tr></table></body></html>';
}

// ---- n8n entry point ----
const settings = $('Check email settings').first().json;
const emails = $('Plan notifications').first().json.emails ?? [];
const rendered = emails.map((email) => render(email, settings));
return (rendered.length ? rendered : [{ none: true, send: false }]).map((json) => ({ json }));
