"""Build the two Lambda artifacts infra/serverless.yml deploys: build/lambda/{app,deps}.zip.

Why a script and not the framework's packaging: serverless.yml lives in infra/, the code in
services/, and the dependency layer has to be built for another platform (Lambda is Linux
arm64, cp314). uv can install that platform's wheels on any machine without Docker
(docs/DECISIONS.md V6). Both zips are deterministic (sorted entries, fixed timestamps), so an
unchanged build has the same hash and a redeploy skips it.

  * app.zip: services/{common,ingest,notify,presign,status} and the RDS CA bundle, which
    common.db verifies the server against (certs/rds-global-bundle.pem, AD10);
  * deps.zip: python/ with psycopg and psycopg-binary 3.3.6 (libpq included), the layer for
    ingest and status. boto3 comes with the runtime (V5).

Usage:
    uv run python scripts/build_lambda.py
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from scripts.envfile import REPO_ROOT

OUT = REPO_ROOT / "build" / "lambda"
PACKAGES = ("common", "ingest", "notify", "presign", "status")
CA_BUNDLE = REPO_ROOT / "certs" / "rds-global-bundle.pem"
CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
DEPS = ("psycopg==3.3.6", "psycopg-binary==3.3.6")
PLATFORM = ("--python-platform", "aarch64-manylinux_2_28", "--python-version", "3.14")
EPOCH = (1980, 1, 1, 0, 0, 0)
# The wheel ships its Cython source and type stubs; Lambda never reads them.
SOURCE_ONLY = {".c", ".pyx", ".pxd", ".pyi"}


def ensure_ca_bundle() -> Path:
    """The public RDS CA bundle, downloaded once into certs/ (gitignored)."""
    if not CA_BUNDLE.exists():
        CA_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(CA_URL, timeout=30) as response:
            data = response.read()
        if data.count(b"-----BEGIN CERTIFICATE-----") < 10:
            raise SystemExit(f"{CA_URL} did not return the RDS certificate bundle")
        CA_BUNDLE.write_bytes(data)
    return CA_BUNDLE


def write_zip(path: Path, files: dict[str, Path]) -> str:
    """Zip `files` (archive name -> source) reproducibly; return the sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name].read_bytes())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def app_files() -> dict[str, Path]:
    """The handlers and their shared code, plus the CA bundle; no tests, no caches."""
    services = REPO_ROOT / "services"
    files = {
        str(p.relative_to(services)): p
        for package in PACKAGES
        for p in (services / package).rglob("*.py")
        if "__pycache__" not in p.parts
    }
    files["certs/rds-global-bundle.pem"] = ensure_ca_bundle()
    return files


def deps_files(target: Path) -> dict[str, Path]:
    """Install the layer's wheels for Lambda's platform into target/python."""
    subprocess.run(
        ["uv", "pip", "install", "--quiet", "--target", str(target / "python"), *PLATFORM,
         "--only-binary", ":all:", *DEPS],
        check=True,
    )  # fmt: skip
    shutil.rmtree(target / "python" / "bin", ignore_errors=True)
    return {
        str(p.relative_to(target)): p
        for p in sorted((target / "python").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in SOURCE_ONLY
    }


def main() -> int:
    """Build both zips and print their sizes and hashes."""
    app = OUT / "app.zip"
    print(f"{app.relative_to(REPO_ROOT)}: {write_zip(app, app_files())[:12]} "
          f"({app.stat().st_size / 1024:.0f} KB)")  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        deps = OUT / "deps.zip"
        digest = write_zip(deps, deps_files(Path(tmp)))
        print(f"{deps.relative_to(REPO_ROOT)}: {digest[:12]} "
              f"({deps.stat().st_size / 1024 / 1024:.1f} MB)")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
