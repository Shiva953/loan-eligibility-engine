"""Upload a CSV to the deployed stack exactly as the page does, and time it to the end.

Why: a browser-free check of the whole AWS path (presign Function URL, a POST straight to S3,
ingest, notify, the tunnel, Workflows B and C, the status Function URL), with each stage's
time. `data/warmup.csv` holds one customer who matches nothing, so it sends no email; any
other file emails the test aliases when EMAIL_MODE=test_aliases.

Usage:
    uv run python scripts/aws_upload.py data/warmup.csv
"""

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from scripts.envfile import REPO_ROOT

OUTPUTS = REPO_ROOT / "build" / "stack-outputs.json"


def _json(request: urllib.request.Request | str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        loaded: dict[str, Any] = json.loads(response.read())
    return loaded


def upload(path: Path, outputs: dict[str, str]) -> str:
    """Presign, then POST the file to S3 as multipart form data; returns the object key."""
    data = path.read_bytes()
    # text/plain keeps it a CORS "simple" request, as the page sends it (AD35).
    target = _json(urllib.request.Request(
        outputs["PresignUrl"], method="POST", headers={"Content-Type": "text/plain"},
        data=json.dumps({"filename": path.name, "size": len(data)}).encode()), 60)  # fmt: skip
    boundary = uuid.uuid4().hex
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in target["fields"].items()
    ) + (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         f'filename="{path.name}"\r\nContent-Type: text/csv\r\n\r\n').encode() + data + (
        f"\r\n--{boundary}--\r\n".encode())  # fmt: skip
    request = urllib.request.Request(
        target["url"], data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})  # fmt: skip
    with urllib.request.urlopen(request, timeout=300):
        pass
    return str(target["fields"]["key"])


def main(argv: list[str] | None = None) -> int:
    """Upload, follow the batch until it is done, and print the timeline."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("file", type=Path)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if not OUTPUTS.exists():
        raise SystemExit("no build/stack-outputs.json: run `make stack-outputs` first")
    outputs = json.loads(OUTPUTS.read_text())

    started = time.perf_counter()
    key = upload(args.file, outputs)
    print(f"{time.perf_counter() - started:6.1f} s  uploaded to S3 as {key}", flush=True)
    last: str | None = None
    status: dict[str, Any] = {}
    while time.perf_counter() - started < args.timeout:
        status = _json(f"{outputs['StatusUrl']}?key={key}", 70)
        if status["status"] != last:
            last = status["status"]
            print(f"{time.perf_counter() - started:6.1f} s  {last}", flush=True)
        if status.get("done"):
            break
        time.sleep(0.5)
    print(json.dumps({k: status.get(k) for k in ("rows", "matching", "emails", "ms", "error")}))
    return 0 if status.get("status") == "notified" else 1


if __name__ == "__main__":
    sys.exit(main())
