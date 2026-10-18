"""Put or delete the SSM parameters that infra/serverless.yml reads (`make aws-params`).

Why outside the stack: CloudFormation can read a SecureString (`{{resolve:ssm-secure:...}}`)
but can't create one, and the tunnel URL changes every time cloudflared restarts, long after a
deploy. Secrets stay in SSM SecureStrings, never in Secrets Manager (cost; CLAUDE.md) or in
the template:
  * <SSM_PREFIX>/rds-master-password (SecureString), from RDS_MASTER_PASSWORD;
  * <SSM_PREFIX>/webhook-secret (SecureString), from WEBHOOK_HMAC_SECRET;
  * <SSM_PREFIX>/tunnel-url (String), from the running quick tunnel.
The values are never printed. AWS credentials come from AWS_PROFILE, as for the deploy.

Usage:
    uv run python scripts/aws_params.py put       # all three (the tunnel must be running)
    uv run python scripts/aws_params.py tunnel    # only the tunnel URL, after a restart
    uv run python scripts/aws_params.py delete    # teardown
"""

import argparse
import sys

import boto3

from scripts.envfile import load_settings, require
from scripts.tunnel_url import from_logs, from_metrics

SECURE = {"rds-master-password": "RDS_MASTER_PASSWORD", "webhook-secret": "WEBHOOK_HMAC_SECRET"}
TUNNEL = "tunnel-url"


def tunnel_url(settings: dict[str, str]) -> str:
    """The running quick tunnel's URL, or a stop with how to start it."""
    url = from_metrics(settings.get("CLOUDFLARED_METRICS_HOST_PORT", "20241")) or from_logs()
    if not url:
        raise SystemExit("no tunnel URL found: start it with `make tunnel`")
    return url


def main(argv: list[str] | None = None) -> int:
    """Put, refresh or delete the parameters."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("action", choices=["put", "tunnel", "delete"])
    args = parser.parse_args(argv)
    settings = load_settings()
    prefix = require(settings, "SSM_PREFIX")[0].rstrip("/")
    session = boto3.Session(profile_name=settings.get("AWS_PROFILE") or None)
    ssm = session.client("ssm", region_name=require(settings, "AWS_REGION")[0])

    if args.action == "delete":
        names = [f"{prefix}/{name}" for name in (*SECURE, TUNNEL)]
        result = ssm.delete_parameters(Names=names)
        print(f"deleted {len(result['DeletedParameters'])}, "
              f"not found {len(result['InvalidParameters'])}")  # fmt: skip
        return 0
    if args.action == "put":
        for name, key in SECURE.items():
            ssm.put_parameter(Name=f"{prefix}/{name}", Value=require(settings, key)[0],
                              Type="SecureString", Overwrite=True, Tier="Standard")  # fmt: skip
            print(f"{prefix}/{name}: SecureString from {key}")
    url = tunnel_url(settings)
    ssm.put_parameter(Name=f"{prefix}/{TUNNEL}", Value=url, Type="String", Overwrite=True,
                      Tier="Standard")  # fmt: skip
    print(f"{prefix}/{TUNNEL}: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
