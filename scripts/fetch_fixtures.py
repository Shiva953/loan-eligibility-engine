"""Save the crawl sources' pages and robots.txt as fixtures, politely.

Why fixtures: Workflow A defaults to ``CRAWL_MODE=fixtures`` so tests, the bench and the demo
never depend on a live site (BankBazaar sits behind Akamai, and Paisabazaar started returning
403 after a handful of requests). This script is the only place the repo fetches those sites
outside the workflow, and it applies the same etiquette: robots.txt checked for our
User-Agent, one request at a time, a pause between requests, and the honest UA from .env.

Each fetch writes ``tests/fixtures/crawl/<slug>/`` plus a ``manifest.json`` that records the
URL, HTTP status, fetch time and SHA-256 of every file, so a refreshed fixture is visible in
review.

Usage:
    uv run python scripts/fetch_fixtures.py               # all enabled sources
    uv run python scripts/fetch_fixtures.py --all         # include disabled backups
    uv run python scripts/fetch_fixtures.py --only kotak
"""

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

from scripts.envfile import REPO_ROOT, load_settings

SOURCES_FILE = REPO_ROOT / "data" / "crawl_sources.json"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "crawl"


def load_sources() -> list[dict[str, Any]]:
    """Read the shared source list."""
    sources: list[dict[str, Any]] = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
    return sources


def fetch(url: str, user_agent: str) -> tuple[int, bytes]:
    """GET one URL, returning the status and body even for HTTP errors."""
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def save_source(source: dict[str, Any], user_agent: str, delay: float) -> list[dict[str, Any]]:
    """Fetch robots.txt and every page of one source; return manifest entries."""
    out_dir = FIXTURE_ROOT / source["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(source["pages"][0]["url"]))
    entries: list[dict[str, Any]] = []

    status, robots_body = fetch(f"{origin}/robots.txt", user_agent)
    (out_dir / "robots.txt").write_bytes(robots_body)
    entries.append(entry("robots.txt", f"{origin}/robots.txt", status, robots_body))
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_body.decode("utf-8", "replace").splitlines())

    for page in source["pages"]:
        time.sleep(delay)
        if not parser.can_fetch(user_agent, page["url"]):
            print(f"  robots.txt disallows {page['url']}: skipped")
            entries.append(
                {"file": page["fixture"], "url": page["url"], "status": "robots_blocked"}
            )
            continue
        status, body = fetch(page["url"], user_agent)
        print(f"  {status} {len(body):>9,} bytes  {page['url']}")
        if status == 200:
            (out_dir / page["fixture"]).write_bytes(body)
        entries.append(entry(page["fixture"], page["url"], status, body))
    return entries


def entry(file: str, url: str, status: int, body: bytes) -> dict[str, Any]:
    """Describe one fetched file for the manifest."""
    return {
        "file": file,
        "url": url,
        "status": status,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    """Fetch the selected sources and write their manifests."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--all", action="store_true", help="include disabled backup sources")
    parser.add_argument("--only", help="fetch one source by slug")
    args = parser.parse_args(argv)

    settings = load_settings()
    user_agent = settings.get("CRAWL_USER_AGENT", "LoanEligibilityEngine/0.1")
    delay = float(settings.get("CRAWL_DELAY_SECONDS", "5"))
    failures = 0
    for source in load_sources():
        if args.only and source["slug"] != args.only:
            continue
        if not (source["enabled"] or args.all or args.only):
            continue
        print(f"{source['slug']}:")
        entries = save_source(source, user_agent, delay)
        manifest = FIXTURE_ROOT / source["slug"] / "manifest.json"
        manifest.write_text(json.dumps({"entries": entries}, indent=2) + "\n", encoding="utf-8")
        failures += sum(1 for e in entries if e["status"] != 200)
        time.sleep(delay)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
