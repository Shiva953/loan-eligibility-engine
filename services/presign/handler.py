"""Presign Lambda (Function URL): a short-lived POST target for one new upload key.

Why: the browser uploads the CSV straight to S3, so the file never passes through a Lambda or
API Gateway payload limit (docs/ASSIGNMENT.md). The key is generated here, the policy allows
exactly that key, `text/csv`, at most 50 MB, for 5 minutes (docs/DECISIONS.md AD8). Signing is
offline and needs no boto3 (common.sigv4), which keeps this cold start short.
"""

import base64
import json
import os
import uuid
from typing import Any

from common.sigv4 import Credentials, presigned_post

MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", 50 * 1024 * 1024))
EXPIRES_IN = int(os.environ.get("PRESIGN_EXPIRES_SECONDS", 300))


def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """A Function URL response with a JSON body."""
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}  # fmt: skip


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """POST {filename, size, gzip?} -> {url, fields}; the page posts the file with the fields.

    With gzip: true the page compresses the CSV before sending it (about 4x smaller, so about
    4x faster from far away), and the policy pins Content-Encoding: gzip. Ingest decompresses.
    """
    raw = event.get("body") or "{}"
    try:
        if event.get("isBase64Encoded"):  # a Function URL encodes bodies it doesn't take as text
            raw = base64.b64decode(raw).decode()
        body = json.loads(raw)
        size = int(body.get("size") or 0)
    except ValueError, TypeError:
        return response(400, {"error": "send JSON {filename, size}"})
    if not 0 < size <= MAX_BYTES:
        return response(400, {"error": "the file must be between 1 byte and 50 MB"})
    key = f"uploads/{uuid.uuid4()}.csv"
    target = presigned_post(
        bucket=os.environ["UPLOAD_BUCKET"], key=key, region=os.environ["AWS_REGION"],
        credentials=Credentials.from_env(), content_type="text/csv", max_bytes=MAX_BYTES,
        expires_in=EXPIRES_IN,
        extra_fields={"Content-Encoding": "gzip"} if body.get("gzip") is True else None,
    )  # fmt: skip
    return response(200, target)
