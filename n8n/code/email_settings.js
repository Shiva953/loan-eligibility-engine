// Code node "Check email settings" (Workflow C). Mode: Run Once for All Items.
//
// Workflow C refuses to run unless the email switches are explicitly safe (DECISIONS AD5):
// EMAIL_MODE is exactly 'suppressed' or 'test_aliases', the aliases are valid and distinct,
// EMAIL_CAP is between 1 and the number of aliases, and the sender is an address. The planning
// query applies the same rules before it records anything (it reads app_config itself, to save
// a round trip) and returns config_error instead; this node then stops the workflow with the
// reason, before anything is rendered or sent. bootstrap validates the same values, so either
// check only fires if app_config was edited by hand.
//
// Input:  one item from "Plan notifications": { config, config_error, ... }
// Output: one item { mode, aliases, cap, sender, pause_seconds }.

const MODES = new Set(['suppressed', 'test_aliases']);
const EMAIL = /^[^@\s,]+@[^@\s,]+\.[^@\s,]+$/;

function emailSettings(config) {
  const mode = String(config.email_mode ?? '');
  if (!MODES.has(mode)) {
    throw new Error(`EMAIL_MODE must be "suppressed" or "test_aliases", not ${JSON.stringify(mode)}: refusing to run`);
  }
  const aliases = String(config.email_aliases ?? '').split(',').map((a) => a.trim()).filter(Boolean);
  if (!aliases.length || !aliases.every((a) => EMAIL.test(a))) {
    throw new Error('EMAIL_ALIASES must be a comma-separated list of email addresses: refusing to run');
  }
  if (new Set(aliases.map((a) => a.toLowerCase())).size !== aliases.length) {
    throw new Error('EMAIL_ALIASES lists the same address twice: refusing to run');
  }
  const cap = Number(config.email_cap);
  if (!Number.isInteger(cap) || cap < 1 || cap > aliases.length) {
    throw new Error(`EMAIL_CAP must be between 1 and ${aliases.length} (the number of aliases): refusing to run`);
  }
  const sender = String(config.ses_sender ?? '');
  if (!EMAIL.test(sender)) throw new Error('SES_SENDER is not an email address: refusing to run');
  const perSecond = Number(config.ses_max_per_second);
  if (!(perSecond > 0)) throw new Error('SES_MAX_PER_SECOND must be a positive number: refusing to run');
  return { mode, aliases, cap, sender, pause_seconds: Math.ceil(1000 / perSecond) / 1000 };
}

function checkPlan(plan) {
  const settings = emailSettings(plan.config ?? {});
  if (plan.config_error) throw new Error(`${plan.config_error}: refusing to run`);
  return settings;
}

// ---- n8n entry point ----
return [{ json: checkPlan($input.first().json) }];
