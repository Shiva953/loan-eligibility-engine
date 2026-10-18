-- Workflow A: the enabled sources, one item each, in a stable order. robots_url is derived
-- here so workflow expressions never have to parse URLs.
SELECT source_id,
       slug,
       display_name,
       kind,
       page_urls,
       fixture_files,
       last_content_hash,
       regexp_replace(page_urls[1], '^(https?://[^/]+).*$', '\1') || '/robots.txt' AS robots_url
FROM crawl_sources
WHERE enabled
ORDER BY source_id;
