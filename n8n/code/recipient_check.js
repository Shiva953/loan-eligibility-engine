// Code node "Check recipients" (Workflow C). Mode: Run Once for All Items.
//
// The last check before the SES node, independent of the planning query that chose the
// recipients. It stops the workflow, sending nothing, unless all of these hold:
//   * EMAIL_MODE is test_aliases;
//   * there are no more emails than EMAIL_CAP;
//   * every recipient is exactly one of EMAIL_ALIASES, and no alias is used twice;
//   * the sender is SES_SENDER.
// The SES sandbox (only verified identities can receive) is the second layer behind this one.

function checkRecipients(emails, settings) {
  if (settings.mode !== 'test_aliases') throw new Error('refusing to send: EMAIL_MODE is not test_aliases');
  if (emails.length > settings.cap) {
    throw new Error(`refusing to send ${emails.length} emails: EMAIL_CAP is ${settings.cap}`);
  }
  const allowed = new Set(settings.aliases.map((a) => a.toLowerCase()));
  const used = new Set();
  for (const email of emails) {
    const to = typeof email.to === 'string' ? email.to.toLowerCase() : '';
    if (!email.send || !allowed.has(to)) {
      throw new Error(`refusing to send to ${JSON.stringify(email.to)}: not one of EMAIL_ALIASES`);
    }
    if (used.has(to)) throw new Error(`refusing to send: ${email.to} would get two emails`);
    used.add(to);
    if (email.from !== settings.sender) throw new Error('refusing to send: the sender is not SES_SENDER');
  }
  return emails;
}

// ---- n8n entry point ----
const settings = $('Check email settings').first().json;
return checkRecipients($input.all().map((item) => item.json), settings).map((json) => ({ json }));
