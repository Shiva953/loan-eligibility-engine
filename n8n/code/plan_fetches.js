// Code node "Plan page fetches" (Workflow A). Mode: Run Once for All Items.
//
// Input: the robots.txt response for the current source (HTTP node, full response).
// Output: one item per page to fetch, or a single { robots_blocked: true } item.
//
// robots.txt is honoured in both modes. In fixtures mode the saved robots.txt is checked
// against the real page URLs, so a fixture run makes the same decision a live run would.
// Matching follows RFC 9309: the group for our product token wins over "*", the longest
// matching rule wins, Allow wins a tie, and "*" / "$" wildcards are supported. A 4xx
// robots.txt means no restrictions. A 5xx or unreachable robots.txt means "assume
// disallowed", as the RFC requires.

function parseRobots(text) {
  const groups = [];
  let current = null;
  let lastWasAgent = false;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.replace(/#.*$/, '').trim();
    const match = line.match(/^([A-Za-z-]+)\s*:\s*(.*)$/);
    if (!match) continue;
    const field = match[1].toLowerCase();
    const value = match[2].trim();
    if (field === 'user-agent') {
      if (!lastWasAgent) {
        current = { agents: [], rules: [] };
        groups.push(current);
      }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
    } else if ((field === 'allow' || field === 'disallow') && current) {
      if (value !== '') current.rules.push({ allow: field === 'allow', path: value });
      lastWasAgent = false;
    } else {
      lastWasAgent = false;
    }
  }
  return groups;
}

function ruleMatches(rulePath, path) {
  const anchored = rulePath.endsWith('$');
  const body = anchored ? rulePath.slice(0, -1) : rulePath;
  const pattern = body
    .split('*')
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&'))
    .join('.*');
  return new RegExp(`^${pattern}${anchored ? '$' : ''}`).test(path);
}

// The Code-node sandbox has no URL global, so the path and query are taken with a regex.
function pathAndQuery(url) {
  const match = String(url).match(/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*([^#]*)/i);
  const rest = match && match[1] ? match[1] : '/';
  const withSlash = rest.startsWith('/') ? rest : `/${rest}`;
  const queryAt = withSlash.indexOf('?');
  const path = queryAt === -1 ? withSlash : withSlash.slice(0, queryAt);
  const query = queryAt === -1 ? '' : withSlash.slice(queryAt);
  try {
    return decodeURIComponent(path) + query;
  } catch {
    return path + query;
  }
}

function isAllowed(groups, productToken, url) {
  const token = productToken.toLowerCase();
  const own = groups.filter((g) => g.agents.some((a) => a !== '*' && token.startsWith(a)));
  const chosen = own.length ? own : groups.filter((g) => g.agents.includes('*'));
  const rules = chosen.flatMap((g) => g.rules);
  const path = pathAndQuery(url);
  let best = null;
  for (const rule of rules) {
    if (!ruleMatches(rule.path, path)) continue;
    const longer = !best || rule.path.length > best.path.length;
    const tieAllow = best && rule.path.length === best.path.length && rule.allow;
    if (longer || tieAllow) best = rule;
  }
  return best ? best.allow : true;
}

function plan(source, config, robotsResponse) {
  const userAgent = config.crawl_user_agent;
  const productToken = userAgent.split('/')[0];
  const status = robotsResponse.statusCode ?? 0;
  if (status === 0) {
    // Network failure, not a robots decision: log it as an error so it can be retried tomorrow.
    return [{
      robots_blocked: true,
      run_status: 'error',
      http_status: null,
      error: `robots.txt unreachable: ${robotsResponse.error?.message ?? 'request failed'}`,
    }];
  }
  let groups;
  // HTTP Request with full response + text format puts the body in `data`.
  if (status >= 200 && status < 300) groups = parseRobots(String(robotsResponse.data ?? ''));
  else if (status >= 400 && status < 500) groups = [];
  else groups = [{ agents: ['*'], rules: [{ allow: false, path: '/' }] }];

  const fixtures = config.crawl_mode === 'fixtures';
  const pages = source.page_urls.map((url, i) => ({
    source_id: source.source_id,
    slug: source.slug,
    url,
    fetch_url: fixtures ? `http://fixtures/${source.slug}/${source.fixture_files[i]}` : url,
    user_agent: userAgent,
  }));
  const blocked = pages.filter((page) => !isAllowed(groups, productToken, page.url));
  if (blocked.length) {
    return [{
      robots_blocked: true,
      run_status: 'robots_blocked',
      http_status: status,
      error: `robots.txt disallows ${blocked.map((p) => p.url).join(', ')}`,
    }];
  }
  return pages.map((page) => ({ robots_blocked: false, ...page }));
}

// ---- n8n entry point ----
const source = $('Loop over sources').first().json;
const config = $('Load app_config').first().json.config;
return plan(source, config, $input.first().json).map((json) => ({ json }));
