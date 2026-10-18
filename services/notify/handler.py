"""Notify Lambda (ingest's onSuccess destination, outside the VPC): wake Workflow B.

It reads the tunnel URL and the webhook secret from SSM (a SecureString), signs the JWT and
POSTs it through Cloudflare to n8n (AD11). A duplicate or failed ingest sends nothing. A
refused or unreachable webhook raises, so Lambda retries; the sweeper on Workflow B is the
final safety net for a batch whose webhook never arrived (AD6).
"""

import os
from typing import Any

from notify.core import send


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """A destination record -> the webhook's HTTP status."""
    result = event.get("responsePayload") or event
    # A duplicate, a failed ingest or a keep-warm ping: nothing to announce, and no boto3 import.
    if result.get("duplicate") or result.get("failed") or not result.get("batch_id"):
        return {"skipped": True, "batch_id": result.get("batch_id")}
    import boto3

    # One call for both (WithDecryption only affects the SecureString).
    names = [os.environ["TUNNEL_URL_PARAM"], os.environ["WEBHOOK_SECRET_PARAM"]]
    found = boto3.client("ssm").get_parameters(Names=names, WithDecryption=True)["Parameters"]
    values = {p["Name"]: p["Value"] for p in found}
    status = send(values[names[0]], values[names[1]], result)
    if status != 202:
        raise RuntimeError(f"webhook for batch {result['batch_id']} returned HTTP {status}")
    return {"batch_id": result["batch_id"], "status": status}
