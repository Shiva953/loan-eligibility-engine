// Code node "Collect pages" (Workflow A). Mode: Run Once for All Items.
//
// Joins the per-page HTTP responses back into one item for the source. Pages are paired with
// their plan by position, which the HTTP Request node preserves. A source is only extracted
// when every page came back 200: a partial source would make products look removed and
// deactivate them a week later.

function collect(source, plans, responses) {
  const pages = plans.map((planned, i) => ({
    url: planned.url,
    status: responses[i]?.statusCode ?? 0,
    // HTTP Request with full response + text format puts the body in `data`.
    body: typeof responses[i]?.data === 'string' ? responses[i].data : '',
    error: responses[i]?.error?.message ?? null,
  }));
  const failed = pages.find((page) => page.status !== 200);
  if (failed) {
    return {
      fetch_ok: false,
      run_status: 'error',
      http_status: failed.status || null,
      error: failed.status ? `HTTP ${failed.status} for ${failed.url}` : `${failed.url}: ${failed.error ?? 'request failed'}`,
    };
  }
  return {
    fetch_ok: true,
    source_id: source.source_id,
    slug: source.slug,
    display_name: source.display_name,
    kind: source.kind,
    http_status: 200,
    pages: pages.map(({ url, body }) => ({ url, body })),
  };
}

// ---- n8n entry point ----
const source = $('Loop over sources').first().json;
const plans = $('Plan page fetches').all().map((item) => item.json);
const responses = $input.all().map((item) => item.json);
return [{ json: collect(source, plans, responses) }];
