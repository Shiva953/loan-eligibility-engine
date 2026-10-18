"""Run the Serverless CLI on infra/serverless.yml with the settings it reads from .env.

Why a wrapper: serverless.yml reads ${env:...} values that live in the repo's .env, and the
framework would otherwise see the whole shell. This passes exactly the keys the template uses
(plus SERVERLESS_ACCESS_KEY for v4's login), so no unrelated secret (the Gemini key, the SES
keys) reaches the framework. The two CLIs are pinned (docs/DECISIONS.md V7):
  * serverless@4.44.0 deploys (the owner's free v4 login);
  * osls@4.4.0 runs `package` without a login, which checks the file stays osls-compatible.
`remove` empties the upload bucket first, since CloudFormation can't delete a bucket with
objects in it.

Usage:
    uv run python scripts/sls.py package             # osls, compiles to infra/.serverless/
    uv run python scripts/sls.py deploy              # ask the owner first
    uv run python scripts/sls.py remove              # ask the owner first
"""

import argparse
import os
import subprocess
import sys

from scripts.envfile import REPO_ROOT, load_settings, require
from scripts.stack import outputs, session

CLIS = {"osls": "osls@4.4.0", "serverless": "serverless@4.44.0"}
REQUIRED = ("AWS_REGION", "SSM_PREFIX", "MY_IP_CIDR")
OPTIONAL = ("AWS_PROFILE", "STAGE", "RDS_DB_NAME", "RDS_MASTER_USER", "UPLOAD_MAX_BYTES",
            "PRESIGN_EXPIRES_SECONDS", "UI_PUBLIC_ORIGIN")  # fmt: skip
PASS_THROUGH = ("PATH", "HOME", "TMPDIR", "TERM", "LANG")


def empty_bucket(settings: dict[str, str]) -> None:
    """Delete every object in the upload bucket, if the stack (and the bucket) still exist."""
    bucket = outputs(settings).get("UploadBucket")
    if bucket:
        session(settings).resource("s3").Bucket(bucket).objects.all().delete()
        print(f"emptied s3://{bucket}")


def main(argv: list[str] | None = None) -> int:
    """Run one framework command in infra/."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=["package", "deploy", "remove", "info"])
    parser.add_argument("--cli", choices=sorted(CLIS))
    args = parser.parse_args(argv)
    settings = load_settings()
    require(settings, *REQUIRED)
    cli = args.cli or ("osls" if args.command == "package" else "serverless")
    if cli == "serverless":
        require(settings, "SERVERLESS_ACCESS_KEY")

    env = {k: os.environ[k] for k in PASS_THROUGH if k in os.environ}
    env.update({k: settings[k] for k in (*REQUIRED, *OPTIONAL) if settings.get(k)})
    if cli == "serverless":
        env["SERVERLESS_ACCESS_KEY"] = settings["SERVERLESS_ACCESS_KEY"]
    if args.command == "remove":
        empty_bucket(settings)
    stage = settings.get("STAGE") or "dev"
    command = ["npx", "--yes", CLIS[cli], args.command, "--stage", stage]
    return subprocess.run(command, cwd=REPO_ROOT / "infra", env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
