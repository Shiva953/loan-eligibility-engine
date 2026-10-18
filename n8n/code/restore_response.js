// Code node "Restore response" (LLM gateway). Mode: Run Once for All Items.
//
// "Log LLM call" is a Postgres node, so its output is the inserted rows. This puts the parsed
// provider responses back on the main path for the retry decision. $('…').all() returns the
// latest run, which is the pass just logged. Requests to retry wait together (one Wait node),
// as long as the longest delay any of them was given, and their next pass starts after it.

const items = $('Parse provider response').all().map((item) => item.json);
const wait = Math.max(0, ...items.filter((item) => item.retry).map((item) => item.wait_seconds ?? 0));
const resend = Date.now() + wait * 1000;
return items.map((item) => ({ json: item.retry ? { ...item, wait_seconds: wait, sent_ms: resend } : item }));
