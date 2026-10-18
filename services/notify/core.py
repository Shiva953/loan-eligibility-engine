"""Tell Workflow B that a batch is ready: a signed JWT webhook (docs/DECISIONS.md AD11).

Shared by the notify Lambda (which reads the URL and secret from SSM) and the local runners
(which read them from .env), so both send exactly the same request.
"""

import json
import urllib.error
import urllib.request
from typing import Any

from common.webhook_jwt import sign_token

WEBHOOK_PATH = "/webhook/loan-match"


def claims(result: dict[str, Any]) -> dict[str, Any]:
    """The facts Workflow B may trust: only what ingest reported, never the request body."""
    return {
        "batch_id": result["batch_id"],
        "rows_valid": result.get("rows_valid"),
        "rows_rejected": result.get("rows_rejected"),
        "source": "notify",
    }


def send(base_url: str, secret: str, result: dict[str, Any], timeout: float = 10) -> int:
    """POST the signed batch-ready token; returns the HTTP status (202 when accepted)."""
    token = sign_token(claims(result), secret)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{WEBHOOK_PATH}",
        data=json.dumps({"batch_id": result["batch_id"]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
