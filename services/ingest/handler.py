"""Ingest Lambda (S3 ObjectCreated on uploads/, in the VPC): load the CSV into Postgres.

The object is read as a stream (no temp copy), rows go through ingest.core, and rejects are
written next to it as rejects/<batch>.csv. The return value is the onSuccess destination's
payload, so notify learns the batch id and counts without a database call (AD2). A file that
can't be ingested at all is recorded as a failed batch; anything else raises, so Lambda retries
the event and finally sends it to the dead-letter queue.
"""

from typing import Any, BinaryIO
from urllib.parse import unquote_plus

from common.db import budget_for, connect, warm
from ingest.core import IngestError, fail, ingest


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """One S3 record -> {batch_id, duplicate, rows_*}; or a keep-warm ping (AD41)."""
    if event.get("warmup"):
        return warm(context)
    import boto3  # only here: the other Lambdas don't pay for importing it

    record = event["Records"][0]["s3"]
    bucket = record["bucket"]["name"]
    key = unquote_plus(record["object"]["key"])
    etag = record["object"].get("eTag", "").strip('"')
    # Log in first: a slow login must not leave the S3 download stream idle long enough to time
    # out. A minute stays reserved for the file itself (a 50 MB upload is about 700k rows).
    conn = connect(budget_for(context, reserve=60, cap=120))
    s3 = boto3.client("s3")
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]

    def save_rejects(rejects_key: str, rows: BinaryIO) -> None:
        s3.upload_fileobj(rows, bucket, rejects_key, ExtraArgs={"ContentType": "text/csv"})

    try:
        result = ingest(conn, body, bucket=bucket, key=key, etag=etag, save_rejects=save_rejects)
    except IngestError as error:
        batch_id = fail(conn, bucket=bucket, key=key, etag=etag, error=str(error))
        return {"batch_id": batch_id, "duplicate": False, "failed": True, "error": str(error)}
    return result.as_dict()
