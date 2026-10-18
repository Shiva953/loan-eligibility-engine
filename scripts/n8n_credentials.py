"""Print the n8n credentials JSON that bootstrap pipes into ``n8n import:credentials``.

Why generated rather than committed: credentials hold secrets, so they're built from ``.env``
at import time and streamed into the container. They are never written into the repository.
n8n encrypts plain credential data on import (checked in the 2.43.6 source).

Safety rule (owner's instruction): the owner's AWS admin keys must never end up in an n8n
credential. The SES credential is refused if its key ID matches any key in the AWS CLI
credentials file or the process environment.

Usage:
    uv run python scripts/n8n_credentials.py --target local > /dev/null   # validate only
"""

import argparse
import configparser
import json
import os
import sys
from pathlib import Path
from typing import Any

from scripts.envfile import load_settings, require
from scripts.n8n_ids import CREDENTIALS

AWS_CREDENTIALS_FILE = Path(
    os.environ.get("AWS_SHARED_CREDENTIALS_FILE", Path.home() / ".aws" / "credentials")
)


def admin_key_ids(credentials_file: Path = AWS_CREDENTIALS_FILE) -> set[str]:
    """Collect every AWS access key ID this machine uses outside n8n."""
    ids: set[str] = set()
    if credentials_file.exists():
        parser = configparser.ConfigParser()
        parser.read(credentials_file)
        for section in parser.sections():
            key_id = parser.get(section, "aws_access_key_id", fallback="").strip()
            if key_id:
                ids.add(key_id)
    env_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    if env_key:
        ids.add(env_key)
    return ids


def ses_credential_data(settings: dict[str, str], forbidden_key_ids: set[str]) -> dict[str, Any]:
    """Build the AWS credential for the SES node, refusing the owner's admin keys.

    Without SES keys in .env (a local-only setup), the credential is stored empty, so workflows
    can still reference it and any send attempt fails.
    """
    key_id = settings.get("SES_AWS_ACCESS_KEY_ID", "").strip()
    secret = settings.get("SES_AWS_SECRET_ACCESS_KEY", "").strip()
    if key_id and key_id in forbidden_key_ids:
        raise SystemExit(
            "SES_AWS_ACCESS_KEY_ID is one of this machine's AWS admin keys. n8n may only hold "
            "the SES-only IAM user's key. Refusing to build credentials."
        )
    if bool(key_id) != bool(secret):
        raise SystemExit("set both SES_AWS_ACCESS_KEY_ID and SES_AWS_SECRET_ACCESS_KEY, or neither")
    return {
        "region": require(settings, "AWS_REGION")[0],
        "accessKeyId": key_id,
        "secretAccessKey": secret,
        "temporaryCredentials": False,
    }


def postgres_credential_data(settings: dict[str, str], target: str) -> dict[str, Any]:
    """Point the app Postgres credential at app-db (local) or RDS, always as n8n_app."""
    if target == "local":
        (password,) = require(settings, "N8N_APP_DB_PASSWORD")
        return {
            "host": "app-db",
            "port": 5432,
            "database": settings.get("APP_DB_NAME", "loans"),
            "user": "n8n_app",
            "password": password,
            "ssl": "disable",
            "allowUnauthorizedCerts": False,
            "maxConnections": 20,
        }
    host, password = require(settings, "RDS_HOST", "RDS_N8N_APP_PASSWORD")
    return {
        "host": host,
        "port": int(settings.get("RDS_PORT", "5432")),
        "database": settings.get("RDS_DB_NAME", "loans"),
        "user": "n8n_app",
        "password": password,
        # Verified TLS: the RDS CA is trusted via NODE_EXTRA_CA_CERTS (AD10).
        "ssl": "require",
        "allowUnauthorizedCerts": False,
        "maxConnections": 20,
    }


def build_credentials(
    settings: dict[str, str], target: str, forbidden_key_ids: set[str]
) -> list[dict[str, Any]]:
    """Return the import payload: one entry per credential in ``scripts/n8n_ids.py``."""
    (hmac_secret,) = require(settings, "WEBHOOK_HMAC_SECRET")
    data: dict[str, tuple[str, dict[str, Any]]] = {
        "app_postgres": ("postgres", postgres_credential_data(settings, target)),
        "gemini_api": (
            "httpHeaderAuth",
            {"name": "x-goog-api-key", "value": settings.get("GEMINI_API_KEY", "")},
        ),
        # HS256 is HMAC-SHA256: the Webhook node verifies signature and expiry natively, so
        # the shared secret never appears in workflow JSON or execution data (AD11).
        "webhook_jwt": (
            "jwtAuth",
            {"keyType": "passphrase", "secret": hmac_secret, "algorithm": "HS256"},
        ),
        "aws_ses": ("aws", ses_credential_data(settings, forbidden_key_ids)),
    }
    return [
        {"id": ref["id"], "name": ref["name"], "type": data[key][0], "data": data[key][1]}
        for key, ref in CREDENTIALS.items()
    ]


def main(argv: list[str] | None = None) -> int:
    """Print the credentials JSON to stdout. Warnings go to stderr, never secrets."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--target", choices=["local", "rds"], default="local")
    args = parser.parse_args(argv)

    settings = load_settings()
    payload = build_credentials(settings, args.target, admin_key_ids())
    if not settings.get("GEMINI_API_KEY"):
        print("warning: GEMINI_API_KEY is empty; LLM calls will fail", file=sys.stderr)
    if not settings.get("SES_AWS_ACCESS_KEY_ID"):
        print("note: SES credential stored empty until the SES-only user exists", file=sys.stderr)
    json.dump(payload, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
