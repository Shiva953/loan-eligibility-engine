"""Serve the upload page locally, standing in for the AWS side (`make ui`).

Why: the page talks to three AWS pieces: the presign Lambda, the S3 bucket (presigned POST)
and the status Lambda. The ingest and notify Lambdas run in between, on the S3 event. Until
phase 3 deploys them, this server plays all five on this machine, with the same code:
  * POST /api/presign returns a POST target and fields, as the presign Lambda will;
  * POST /api/upload accepts the multipart form the page sends to S3, saves the file under
    build/uploads/, then runs services/ingest/core.py and services/notify/core.py in a
    thread, as the S3 event and the Lambda destination will;
  * GET /api/status?key=... runs services/status/core.py.
The database work runs as lambda_app, the Lambdas' own role, so a missing grant shows up here.
With UI_TARGET=aws, `config.js` points the same page at the deployed Function URLs and the
bucket instead (from build/stack-outputs.json, written by `make stack-outputs`).

The page itself is the Next.js static export in ui/out (`make ui-build`). In development,
`make ui-dev` runs `next dev` on port 3000, which forwards /config.js and /api/* here.

Usage:
    uv run python scripts/ui_server.py            # http://localhost:8090
"""

import argparse
import contextlib
import email.parser
import email.policy
import errno
import hashlib
import json
import mimetypes
import sys
import threading
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlparse

import psycopg

from ingest.core import IngestError, fail, ingest
from notify.core import send
from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import REPO_ROOT, load_settings, require
from status.core import batch_status

UI_DIR = REPO_ROOT / "ui" / "out"  # the Next.js static export (`make ui-build`)
UPLOADS = REPO_ROOT / "build" / "uploads"
BUCKET = "local-uploads"
MAX_BYTES = 50 * 1024 * 1024
STACK_OUTPUTS = REPO_ROOT / "build" / "stack-outputs.json"
SETTINGS = load_settings()


def page_config() -> dict[str, str]:
    """Where the page sends its calls: these stand-ins, or (UI_TARGET=aws) the deployed stack."""
    if SETTINGS.get("UI_TARGET") != "aws":
        return {"presignUrl": "/api/presign", "statusUrl": "/api/status", "uploadOrigin": "",
                "target": "local"}  # fmt: skip
    if not STACK_OUTPUTS.exists():
        raise SystemExit("UI_TARGET=aws needs build/stack-outputs.json: run `make stack-outputs`")
    outputs = json.loads(STACK_OUTPUTS.read_text())
    return {"presignUrl": outputs["PresignUrl"], "statusUrl": outputs["StatusUrl"],
            "uploadOrigin": outputs["UploadOrigin"], "target": "aws",
            "region": SETTINGS.get("AWS_REGION", "")}  # fmt: skip


def lambda_connection() -> psycopg.Connection:
    """A connection as lambda_app, the role the ingest and status Lambdas use."""
    kwargs = admin_connect_kwargs(SETTINGS, "local")
    kwargs.update(user="lambda_app", password=require(SETTINGS, "LAMBDA_APP_DB_PASSWORD")[0])
    return psycopg.connect(**kwargs)


def save_rejects(key: str, data: BinaryIO) -> None:
    """Where the ingest Lambda writes rejects/<batch>.csv to S3, write it under build/."""
    path = UPLOADS / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.read())


def run_pipeline(key: str, path: Path) -> None:
    """What the S3 event starts: ingest, then (Lambda destination) notify."""
    etag = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    with lambda_connection() as conn:
        try:
            with path.open("rb") as raw:
                result = ingest(
                    conn, raw, bucket=BUCKET, key=key, etag=etag, save_rejects=save_rejects
                )
        except IngestError as error:
            fail(conn, bucket=BUCKET, key=key, etag=etag, error=str(error))
            return
    if result.duplicate:
        return
    gateway = f"http://127.0.0.1:{SETTINGS.get('WEBHOOK_GW_HOST_PORT', '8088')}"
    status = send(gateway, require(SETTINGS, "WEBHOOK_HMAC_SECRET")[0], result.as_dict())
    print(f"ingested {key}: {result.rows_valid} valid, {result.rows_rejected} rejected in "
          f"{result.ms} ms; webhook HTTP {status}", flush=True)  # fmt: skip


def parse_form(content_type: str, body: bytes) -> dict[str, Any]:
    """The fields and the file of a multipart/form-data body (the cgi module is gone in 3.13)."""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    form: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        form[str(name)] = data if part.get_filename() else data.decode()
    return form


class Handler(BaseHTTPRequestHandler):
    """Static files plus the three API stand-ins."""

    server_version = "loan-engine-ui"

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: the poll loop would otherwise print a line every few hundred milliseconds."""

    def reply(self, status: int, body: bytes = b"", content_type: str = "application/json",
              cache: str = "no-store") -> None:  # fmt: skip
        """Send one response with an explicit length (keep-alive friendly)."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """The page, its assets, config.js and the status endpoint."""
        url = urlparse(self.path)
        if url.path == "/api/status":
            key = parse_qs(url.query).get("key", [""])[0]
            with lambda_connection() as conn:
                status = batch_status(conn, key=key)
            self.reply(HTTPStatus.OK, json.dumps(status, default=str).encode())
            return
        if url.path == "/config.js":
            config = page_config()
            self.reply(HTTPStatus.OK, f"window.APP_CONFIG = {json.dumps(config)};\n".encode(),
                       "text/javascript")  # fmt: skip
            return
        name = "index.html" if url.path in ("", "/") else url.path.lstrip("/")
        path = (UI_DIR / name).resolve()
        if not path.is_file() or not path.is_relative_to(UI_DIR.resolve()):
            missing = UI_DIR / "404.html"
            body = missing.read_bytes() if missing.is_file() else b"not found"
            self.reply(HTTPStatus.NOT_FOUND, body, "text/html; charset=utf-8")
            return
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Next fingerprints everything under _next/static, so it can be cached for good.
        immutable = name.startswith("_next/static/")
        cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
        self.reply(HTTPStatus.OK, path.read_bytes(), kind, cache)

    def do_POST(self) -> None:
        """Presign, and the upload that S3 would receive."""
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if url.path == "/api/presign":
            try:
                gzip = json.loads(self.rfile.read(length) or b"{}").get("gzip") is True
            except ValueError, AttributeError:
                gzip = False
            key = f"uploads/{uuid.uuid4()}.csv"
            fields = {"key": key, "Content-Type": "text/csv"}
            if gzip:  # as the presign Lambda: the page sends the CSV gzipped; ingest decompresses
                fields["Content-Encoding"] = "gzip"
            target = {"url": "/api/upload", "fields": fields}
            self.reply(HTTPStatus.OK, json.dumps(target).encode())
            return
        if url.path == "/api/upload":
            if length > MAX_BYTES + 64 * 1024:
                self.reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"<Message>Over 50 MB</Message>",
                           "application/xml")  # fmt: skip
                return
            form = parse_form(self.headers.get("Content-Type", ""), self.rfile.read(length))
            key, data = str(form.get("key", "")), form.get("file")
            if not key.startswith("uploads/") or not isinstance(data, bytes):
                self.reply(HTTPStatus.BAD_REQUEST, b"<Message>Bad upload form</Message>",
                           "application/xml")  # fmt: skip
                return
            path = UPLOADS / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.reply(HTTPStatus.NO_CONTENT)
            threading.Thread(target=self.safely, args=(key, path), daemon=True).start()
            return
        self.reply(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    @staticmethod
    def safely(key: str, path: Path) -> None:
        """Run the pipeline and print any error, as CloudWatch would log it."""
        try:
            run_pipeline(key, path)
        except Exception:
            traceback.print_exc()


def main(argv: list[str] | None = None) -> int:
    """Serve until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--api-only", action="store_true",
                        help="only the API stand-ins and config.js (for `next dev`)")  # fmt: skip
    args = parser.parse_args(argv)
    page_config()  # fail now, not on the first request, if UI_TARGET=aws has no outputs
    if not args.api_only and not (UI_DIR / "index.html").is_file():
        print("no build in ui/out: run `make ui-build` (or `make ui`, which builds first)",
              file=sys.stderr)  # fmt: skip
        return 1
    port = int(SETTINGS.get("UI_HOST_PORT", "8090"))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        print(f"port {port} is already in use, probably by another `make ui`. Stop that one "
              f"(lsof -nP -iTCP:{port} -sTCP:LISTEN shows it), or set UI_HOST_PORT in .env.",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"upload page at http://localhost:{port} (Ctrl-C to stop)", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
