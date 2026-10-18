"""Status Lambda (Function URL, in the VPC): the upload page polls this for its batch.

One query per poll (status.core) on the container's reused connection (common.db).
"""

import json
from typing import Any

from common.db import budget_for, connect, warm
from status.core import batch_status


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """GET ?key=uploads/<id>.csv -> the batch's timeline; or a keep-warm ping (AD41)."""
    if event.get("warmup"):
        return warm(context, cap=50)
    key = (event.get("queryStringParameters") or {}).get("key", "")
    if not key.startswith("uploads/"):
        return {"statusCode": 400, "body": json.dumps({"error": "pass ?key=uploads/..."})}
    conn = connect(budget_for(context, reserve=5, cap=50))
    with conn.transaction():
        status = batch_status(conn, key=key)
    return {"statusCode": 200, "headers": {"Content-Type": "application/json",
                                           "Cache-Control": "no-store"},
            "body": json.dumps(status, default=str)}  # fmt: skip
