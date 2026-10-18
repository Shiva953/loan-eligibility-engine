// Code node "Clean page text" (Workflow A). Mode: Run Once for All Items.
//
// Turns each fetched page into compact, structure-preserving text before hashing and
// extraction. Why not n8n's HTML node: the source pages have no <main> element and use
// generated IDs, so CSS selectors would break on the next redeploy. Plain text extraction
// would also flatten the rate and eligibility tables the LLM most needs. This cleaner keeps
// table rows as "cell | cell" lines and headings as "##" lines, and drops chrome
// (scripts, nav, header, footer, forms) and duplicate lines.
//
// The hash is taken over this text, not the raw HTML, so cosmetic markup changes (tracking
// IDs, build hashes) don't trigger a new LLM extraction.
//
// Input items:  { source_id, slug, pages: [{ url, body }] }
// Output items: { ...input without page bodies, text, text_chars, raw_chars }

const DROP_BLOCKS = [
  'script', 'style', 'noscript', 'svg', 'head', 'nav', 'header', 'footer',
  'form', 'iframe', 'button', 'select', 'template',
];

const NAMED_ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', ndash: '-', mdash: '-',
  rsquo: "'", lsquo: "'", rdquo: '"', ldquo: '"', hellip: '...', bull: '-', middot: '-',
  rupee: '₹', inr: '₹', times: 'x', percnt: '%',
};

function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, code) => {
    if (code[0] === '#') {
      const value = code[1].toLowerCase() === 'x' ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return Number.isFinite(value) ? String.fromCodePoint(value) : match;
    }
    const named = NAMED_ENTITIES[code.toLowerCase()];
    return named === undefined ? match : named;
  });
}

function htmlToText(html) {
  let s = html.replace(/<!--[\s\S]*?-->/g, ' ');
  for (const tag of DROP_BLOCKS) {
    s = s.replace(new RegExp(`<${tag}\\b[\\s\\S]*?</${tag}>`, 'gi'), ' ');
  }
  // Each table row becomes one "cell | cell" line. Cells often wrap their text in <p> or <div>,
  // which would otherwise split a row across lines and detach a lender's name from its rate.
  s = s.replace(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi, (_, row) => {
    const cells = row
      .split(/<\/t[dh]>/i)
      .map((cell) => cell.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
    return `\n${cells.join(' | ')}\n`;
  });
  s = s
    .replace(/<h[1-6]\b[^>]*>/gi, '\n## ')
    .replace(/<li\b[^>]*>/gi, '\n- ')
    .replace(/<(br|p|div|section|article|ul|ol|table|tr|dt|dd)\b[^>]*>/gi, '\n')
    .replace(/<\/(p|div|section|article|ul|ol|table|tr|li|dt|dd|h[1-6])>/gi, '\n')
    .replace(/<[^>]+>/g, ' ');
  // Some CMSs (HDFC's included) ship entity-escaped HTML as text. Decoding reveals that markup,
  // so strip tags a second time and decode again.
  s = decodeEntities(decodeEntities(s).replace(/<[^>]+>/g, ' '));

  const lines = s
    .split('\n')
    .map((raw) =>
      raw
        .replace(/[ \t ]+/g, ' ')
        .replace(/(\s*\|\s*)+$/, '')
        .replace(/^(\s*\|\s*)+/, '')
        .trim(),
    )
    .filter((line) => line.length >= 2 && !/^[-#|\s]+$/.test(line));

  // Menus go first: if they were deduplicated first, a menu entry such as "SBI Personal Loan"
  // would knock out the real heading or table row with the same text further down.
  const seen = new Set();
  const kept = [];
  for (const line of dropLinkFarms(lines)) {
    // Dedupe on the content, so "- Age: 21 to 60" and "Age: 21 to 60" count as one line.
    const key = line.replace(/^(- |## )/, '').toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(line);
  }
  return kept.join('\n');
}

// Mega-menus survive outside <nav> as long runs of short bullets ("- Car Loan", "- PMAY").
// Eligibility bullets almost always carry a number, a percentage or a rupee amount, so a run
// of 4+ short bullets with none of those is navigation and is dropped.
const HAS_FIGURE = /[0-9%₹]/;
function dropLinkFarms(lines) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length < 4) out.push(...run);
    run = [];
  };
  for (const line of lines) {
    const isMenuLike = line.startsWith('- ') && line.length <= 40 && !HAS_FIGURE.test(line);
    if (isMenuLike) {
      run.push(line);
    } else {
      flush();
      out.push(line);
    }
  }
  flush();
  return out;
}

function cleanSource(item) {
  const pages = item.pages || [];
  const sections = pages.map((page) => `=== PAGE: ${page.url} ===\n${htmlToText(page.body || '')}`);
  const text = sections.join('\n\n');
  const { pages: _dropped, ...rest } = item;
  return {
    ...rest,
    page_urls: pages.map((page) => page.url),
    text,
    text_chars: text.length,
    raw_chars: pages.reduce((sum, page) => sum + (page.body || '').length, 0),
  };
}

// ---- n8n entry point ----
return $input.all().map((item) => ({ json: cleanSource(item.json) }));
