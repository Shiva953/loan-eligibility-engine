"""Ingest a CSV from disk exactly as the ingest Lambda does, then optionally run the rest.

Why: before phase 3 there is no bucket and no Lambda. This runs services/ingest/core.py as
lambda_app (the Lambdas' role) on a local file, prints what it stored and rejected and how
long it took, and with --notify sends the signed webhook the notify Lambda would send. --wait
then follows the batch through Workflow B and C. That makes `make demo-local` the whole
pipeline from a CSV to the notifications table.

Usage:
    uv run python scripts/ingest_local.py data/users.csv
    uv run python scripts/ingest_local.py data/users.csv --notify --wait
"""

import argparse
import hashlib
import sys
import time
from pathlib import Path
from typing import BinaryIO

import psycopg

from ingest.core import IngestError, fail, ingest
from notify.core import send
from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import REPO_ROOT, load_settings, require
from status.core import batch_status


def main(argv: list[str] | None = None) -> int:
    """Ingest one file; optionally notify and follow."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("file", type=Path)
    parser.add_argument("--notify", action="store_true", help="send the batch-ready webhook")
    parser.add_argument("--wait", action="store_true", help="follow the batch until notified")
    args = parser.parse_args(argv)

    settings = load_settings()
    kwargs = admin_connect_kwargs(settings, "local")
    kwargs.update(user="lambda_app", password=require(settings, "LAMBDA_APP_DB_PASSWORD")[0])
    data = args.file.read_bytes()
    etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
    key = f"local/{args.file.name}"
    rejects_dir = REPO_ROOT / "build" / "uploads"

    def save_rejects(rejects_key: str, rows: BinaryIO) -> None:
        path = rejects_dir / rejects_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rows.read())

    with psycopg.connect(**kwargs) as conn:
        try:
            with args.file.open("rb") as raw:
                result = ingest(conn, raw, bucket="local", key=key, etag=etag,
                                save_rejects=save_rejects)  # fmt: skip
        except IngestError as error:
            batch = fail(conn, bucket="local", key=key, etag=etag, error=str(error))
            print(f"refused {args.file}: {error} (batch {batch}, status failed)")
            return 1
        if result.duplicate:
            print(f"already ingested: batch {result.batch_id} (same file contents; nothing to do)")
            return 0
        rate = result.rows_total / max(result.ms, 1) * 1000
        print(f"batch {result.batch_id}: {result.rows_total:,} rows in {result.ms} ms "
              f"({rate:,.0f} rows/s) -> {result.rows_valid:,} stored, "
              f"{result.rows_rejected:,} rejected")  # fmt: skip
        if result.rejects_key:
            print(f"rejects: build/uploads/{result.rejects_key}")
        if not args.notify:
            return 0
        gateway = f"http://127.0.0.1:{settings.get('WEBHOOK_GW_HOST_PORT', '8088')}"
        sent = time.perf_counter()
        status = send(gateway, require(settings, "WEBHOOK_HMAC_SECRET")[0], result.as_dict())
        print(f"webhook: HTTP {status}")
        if not args.wait or status != 202:
            return 0 if status == 202 else 1
        while True:
            state = batch_status(conn, batch_id=result.batch_id)
            if state["done"]:
                break
            time.sleep(0.25)
        took = time.perf_counter() - sent
        m, e = state["matching"], state["emails"]
        print(f"{state['status']} {took:.2f} s after the webhook: {m['pairs']:,} pairs, "
              f"{m['llm_calls']} LLM calls; emails {e}")  # fmt: skip
        return 0 if state["status"] == "notified" else 1


if __name__ == "__main__":
    sys.exit(main())
