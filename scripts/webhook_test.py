"""Send a signed (or deliberately bad) batch-ready webhook, the way the notify Lambda will.

Why: it proves the whole receiving path without AWS. The request goes through webhook-gw (or
the tunnel), then n8n's JWT check, then Workflow B. Pass ``--url`` to go through the tunnel
instead of the local gateway port.

Usage:
    uv run python scripts/webhook_test.py                 # valid token: expect 202
    uv run python scripts/webhook_test.py --mode unsigned # expect 403
    uv run python scripts/webhook_test.py --mode bad-signature
    uv run python scripts/webhook_test.py --mode expired
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from common.webhook_jwt import sign_token
from scripts.envfile import load_settings, require

WEBHOOK_PATH = "/webhook/loan-match"


def build_request(url: str, mode: str, secret: str, batch_id: str) -> urllib.request.Request:
    """Build the POST request with the Authorization header the chosen mode calls for."""
    claims = {"batch_id": batch_id, "source": "webhook_test"}
    headers = {"Content-Type": "application/json"}
    if mode == "valid":
        headers["Authorization"] = f"Bearer {sign_token(claims, secret)}"
    elif mode == "bad-signature":
        headers["Authorization"] = f"Bearer {sign_token(claims, secret + 'x')}"
    elif mode == "expired":
        stale = int(time.time()) - 3600
        headers["Authorization"] = f"Bearer {sign_token(claims, secret, now=stale)}"
    body = json.dumps({"batch_id": batch_id}).encode()
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def main(argv: list[str] | None = None) -> int:
    """Send one request and print the HTTP status n8n returned."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--mode", choices=["valid", "unsigned", "bad-signature", "expired"], default="valid"
    )
    parser.add_argument("--url", help="base URL, e.g. the tunnel URL (default: local webhook-gw)")
    parser.add_argument("--batch-id", default=str(uuid.uuid4()))
    args = parser.parse_args(argv)

    settings = load_settings()
    (secret,) = require(settings, "WEBHOOK_HMAC_SECRET")
    base = args.url or f"http://127.0.0.1:{settings.get('WEBHOOK_GW_HOST_PORT', '8088')}"
    request = build_request(base.rstrip("/") + WEBHOOK_PATH, args.mode, secret, args.batch_id)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, text = response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode(errors="replace")
    print(f"{args.mode}: HTTP {status} {text[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
