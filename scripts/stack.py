"""The deployed stack from this machine: its outputs, and stopping or starting RDS.

Why: the upload page needs the two Function URLs and the bucket's origin, and the n8n
credential and migrations need the database host. These live in the stack's outputs, so
`outputs` writes them to build/stack-outputs.json (read by `make ui UI_TARGET=aws`) and prints
the RDS_HOST line for .env. RDS bills by the hour while it runs, so `stop` and `start` keep it
off between sessions (CLAUDE.md). AWS starts a stopped instance again after 7 days.

`warm` logs the in-VPC Lambdas in to RDS right away (docs/DECISIONS.md AD41): a fresh
container's first IAM login can take tens of seconds, and after a deploy or an RDS start every
container is fresh. The 5-minute keep-warm schedule does the same within minutes; this makes
the very first upload fast too.

Usage:
    uv run python scripts/stack.py outputs
    uv run python scripts/stack.py stop|start|state
    uv run python scripts/stack.py ready     # what `make use-rds` runs first (ends with warm)
    uv run python scripts/stack.py warm      # what `make deploy` and `make rds-start` end with
"""

import argparse
import json
import sys
import time
import urllib.request

import boto3

from scripts.envfile import REPO_ROOT, load_settings, require

OUTPUTS = REPO_ROOT / "build" / "stack-outputs.json"


def session(settings: dict[str, str]) -> boto3.Session:
    """A session with the deploy's profile and region."""
    return boto3.Session(profile_name=settings.get("AWS_PROFILE") or None,
                         region_name=require(settings, "AWS_REGION")[0])  # fmt: skip


def stack_name(settings: dict[str, str]) -> str:
    """The framework's stack name, <service>-<stage>; the RDS instance has the same name."""
    return f"loan-engine-{settings.get('STAGE') or 'dev'}"


def outputs(settings: dict[str, str]) -> dict[str, str]:
    """The stack's outputs as {key: value}."""
    stack = session(settings).client("cloudformation").describe_stacks(
        StackName=stack_name(settings))["Stacks"][0]  # fmt: skip
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def ready(settings: dict[str, str], rds: boto3.client, instance: str) -> int:
    """Before connecting from here: is this machine allowed in, and is RDS up (wait if starting).

    Why: a stopped or still-starting instance, or a home IP that changed since MY_IP_CIDR was
    set, both look like a bare connection timeout. This says which one it is.
    """
    allowed = settings.get("MY_IP_CIDR", "")
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as response:
        current = f"{response.read().decode().strip()}/32"
    if current != allowed:
        print(f"your public IP is now {current}, but RDS allows {allowed or 'nothing'}: set "
              f"MY_IP_CIDR={current} in .env and run `make deploy` to update the security group",
              file=sys.stderr)  # fmt: skip
        return 1
    state = rds.describe_db_instances(DBInstanceIdentifier=instance)["DBInstances"][0]
    status = state["DBInstanceStatus"]
    if status in ("stopped", "stopping"):
        print(f"{instance} is {status}: run `make rds-start` first", file=sys.stderr)
        return 1
    if status != "available":
        print(f"{instance} is {status}; waiting until it is available (usually 5-10 minutes) ...",
              flush=True)  # fmt: skip
        rds.get_waiter("db_instance_available").wait(
            DBInstanceIdentifier=instance, WaiterConfig={"Delay": 15, "MaxAttempts": 80}
        )
    print(f"{instance}: available, and {current} is allowed in")
    return warm(settings)


def warm(settings: dict[str, str]) -> int:
    """Invoke ingest and status with a keep-warm ping and wait: each logs in to RDS now."""
    client = session(settings).client("lambda")
    stage = settings.get("STAGE") or "dev"
    failed = 0
    for name in ("ingest", "status"):
        started = time.perf_counter()
        reply = client.invoke(FunctionName=f"loan-engine-{stage}-{name}",
                              Payload=json.dumps({"warmup": True}).encode())  # fmt: skip
        body = json.loads(reply["Payload"].read() or b"{}")
        took = time.perf_counter() - started
        ok = bool(body.get("warm")) and "FunctionError" not in reply
        failed += 0 if ok else 1
        print(f"warm {name}: {'ok' if ok else 'FAILED'} in {took:.1f} s {body}")
    return 1 if failed else 0


def status_of(rds: boto3.client, instance: str) -> str:
    """The instance's current status, such as available, stopping or stopped."""
    return str(rds.describe_db_instances(DBInstanceIdentifier=instance)["DBInstances"][0]
               ["DBInstanceStatus"])  # fmt: skip


def wait_until(rds: boto3.client, instance: str, wanted: str, minutes: int = 20) -> None:
    """Poll every 15 s until the instance reaches `wanted` (stopping and starting take minutes)."""
    status = status_of(rds, instance)
    print(f"{instance} is {status}; waiting until it is {wanted} ...", flush=True)
    for _ in range(minutes * 4):
        if status == wanted:
            return
        time.sleep(15)
        status = status_of(rds, instance)
    raise SystemExit(f"{instance} is still {status} after {minutes} minutes")


def start(rds: boto3.client, instance: str) -> None:
    """Start RDS from any state: wait out a stop in progress; do nothing if it's up or starting.

    Why: AWS refuses StartDBInstance unless the instance is fully stopped, and a stop takes a
    few minutes, so `make rds-stop` followed soon by `make rds-start` used to fail.
    """
    status = status_of(rds, instance)
    if status in ("available", "starting"):
        return
    if status == "stopping":
        wait_until(rds, instance, "stopped")
    rds.start_db_instance(DBInstanceIdentifier=instance)


def stop(rds: boto3.client, instance: str) -> None:
    """Stop RDS from any state: wait out a start in progress; do nothing if already stopping."""
    status = status_of(rds, instance)
    if status in ("stopped", "stopping"):
        return
    if status == "starting":
        wait_until(rds, instance, "available")
    rds.stop_db_instance(DBInstanceIdentifier=instance)


def main(argv: list[str] | None = None) -> int:
    """Write the outputs, or stop, start or show the database."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("action",
                        choices=["outputs", "stop", "start", "state", "ready", "warm"])  # fmt: skip
    args = parser.parse_args(argv)
    settings = load_settings()

    if args.action == "outputs":
        values = outputs(settings)
        OUTPUTS.parent.mkdir(parents=True, exist_ok=True)
        OUTPUTS.write_text(json.dumps(values, indent=2) + "\n")
        print(f"wrote {OUTPUTS.relative_to(REPO_ROOT)}")
        for key in ("PresignUrl", "StatusUrl", "UploadOrigin"):
            print(f"  {key}: {values.get(key)}")
        print(f"set in .env: RDS_HOST={values.get('DatabaseHost')}")
        return 0
    if args.action == "warm":
        return warm(settings)
    rds = session(settings).client("rds")
    instance = stack_name(settings)
    if args.action == "ready":
        return ready(settings, rds, instance)
    if args.action == "stop":
        stop(rds, instance)
    elif args.action == "start":
        start(rds, instance)
        wait_until(rds, instance, "available")
        print(f"{instance}: available")
        return warm(settings)
    print(f"{instance}: {status_of(rds, instance)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
