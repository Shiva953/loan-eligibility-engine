"""Upsert ``data/crawl_sources.json`` into the ``crawl_sources`` table.

Why a seed and not a migration: sources are configuration that changes (a new lender, a moved
page), and migrations are immutable once applied. The upsert is keyed on ``slug`` and never
touches ``last_content_hash``, so re-seeding doesn't force a new LLM extraction.

Usage:
    uv run python scripts/seed_sources.py [--target local|rds] [--reset-hashes]
"""

import argparse
import sys

import psycopg

from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import load_settings
from scripts.fetch_fixtures import load_sources

UPSERT = """
INSERT INTO crawl_sources (slug, display_name, kind, page_urls, fixture_files,
                           content_selector, enabled)
VALUES (%(slug)s, %(display_name)s, %(kind)s, %(page_urls)s, %(fixture_files)s,
        %(content_selector)s, %(enabled)s)
ON CONFLICT (slug) DO UPDATE SET
    display_name     = EXCLUDED.display_name,
    kind             = EXCLUDED.kind,
    page_urls        = EXCLUDED.page_urls,
    fixture_files    = EXCLUDED.fixture_files,
    content_selector = EXCLUDED.content_selector,
    enabled          = EXCLUDED.enabled
"""


def main(argv: list[str] | None = None) -> int:
    """Seed the sources; with --reset-hashes, force the next crawl to re-extract every page."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--target", choices=["local", "rds"], default="local")
    parser.add_argument("--database", help="override the database name (tests use this)")
    parser.add_argument(
        "--reset-hashes",
        action="store_true",
        help="forget every page's content hash, so the next crawl calls the LLM again",
    )
    args = parser.parse_args(argv)

    rows = [
        {
            "slug": s["slug"],
            "display_name": s["display_name"],
            "kind": s["kind"],
            "page_urls": [p["url"] for p in s["pages"]],
            "fixture_files": [p["fixture"] for p in s["pages"]],
            "content_selector": s.get("content_selector"),
            "enabled": s["enabled"],
        }
        for s in load_sources()
    ]
    settings = load_settings()
    with psycopg.connect(**admin_connect_kwargs(settings, args.target, args.database)) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
        if args.reset_hashes:
            conn.execute("UPDATE crawl_sources SET last_content_hash = NULL")
    enabled = sum(1 for r in rows if r["enabled"])
    print(f"crawl_sources: {len(rows)} seeded ({enabled} enabled)")
    if args.reset_hashes:
        print("content hashes cleared: the next crawl re-extracts every page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
