// Code node "Read SES result" (Workflow C). Mode: Run Once for All Items.
//
// The SES node returns only SES's response (or, on its error output, the error), so the email
// it was sending comes from the loop (one email per pass). An email counts as sent only if SES
// returned a message id.
//
// Output: one item { notification_id, outcome: 'sent' | 'failed', ses_message_id, error }.

function sesOutcome(response) {
  const id = response?.SendEmailResponse?.SendEmailResult?.MessageId
    ?? response?.SendEmailResult?.MessageId
    ?? response?.MessageId
    ?? null;
  if (response?.error || !id) {
    const error = response?.message ?? response?.error?.message
      ?? (response?.error ? JSON.stringify(response.error) : 'SES returned no message id');
    return { outcome: 'failed', ses_message_id: '', error: String(error) };
  }
  return { outcome: 'sent', ses_message_id: String(id), error: '' };
}

// ---- n8n entry point ----
const email = $('Loop over emails').first().json;
return [{ json: { notification_id: email.notification_id, ...sesOutcome($input.first().json) } }];
