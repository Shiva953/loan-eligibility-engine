"""End-to-end check of Workflow B: load a batch, send the signed webhook, verify the matches.

Why: Workflow B starts where ingestion ends: an 'ingested' batch plus a signed webhook. Until
the ingest and notify Lambdas exist (phase 2b), this script plays their part. It loads users
(the real CSV or N generated users) exactly as ingest will leave them, signs the webhook the
way notify will, and then checks n8n's result against the brute-force reference: every pair
that passes the hard rules must be in `matches` (recall 1.0) and nothing else.

Usage:
    uv run python scripts/match_e2e.py --csv data/users.csv
    uv run python scripts/match_e2e.py --n 100000 --profile real
    uv run python scripts/match_e2e.py --database loans_bench --n 10000   # after `make use-bench`
    uv run python scripts/match_e2e.py --n 1000 --editor   # watch it run on Workflow B's canvas
    uv run python scripts/match_e2e.py --n 1000 --load-only   # `make load-batch`: load, no webhook

--load-only leaves the batch 'ingested', as ingestion does when its webhook is lost. Workflow
B's "Run manually" trigger (or the sweeper, later) then matches it.

--editor: clicking "Execute workflow" on a webhook-triggered workflow doesn't run it; n8n
listens on the test URL (/webhook-test/loan-match) for one call, for 2 minutes, and runs that
call on the open canvas (mode 'manual'). This sends the signed request there, straight to n8n
on localhost (webhook-gw and the tunnel only ever expose the production /webhook/ path).
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from bench import benchdb, reference
from common.webhook_jwt import sign_token
from scripts.envfile import load_settings, require
from scripts.gen_users import generate
from scripts.n8n_execution import fetch_execution, node_runs
from scripts.n8n_ids import WORKFLOWS


def send_webhook(
    batch_id: str, settings: dict[str, str], base_url: str | None, editor: bool = False
) -> int:
    """POST a signed batch-ready token, as the notify Lambda will; return the HTTP status.

    editor=True sends it to the editor's test URL instead (see the module docstring).
    """
    (secret,) = require(settings, "WEBHOOK_HMAC_SECRET")
    if editor:
        base, path = f"http://127.0.0.1:{settings.get('N8N_HOST_PORT', '5678')}", "/webhook-test"
    else:
        base = base_url or f"http://127.0.0.1:{settings.get('WEBHOOK_GW_HOST_PORT', '8088')}"
        path = "/webhook"
    token = sign_token({"batch_id": batch_id, "source": "match_e2e"}, secret)
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}/loan-match",
        data=json.dumps({"batch_id": batch_id}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def execution_profile(mode: str = "webhook") -> tuple[str, int, str, list[tuple[str, int]]]:
    """Workflow B's latest execution in `mode`: its id, the busiest node, each node's run time.

    The busiest node's item count is what n8n held at once, so it must stay at most the number
    of LLM requests, never the number of users. Editor runs are stored with mode 'manual'.
    """
    exec_id, data = fetch_execution(WORKFLOWS["b_user_matching"], None, mode=mode)
    busiest = max(node_runs(data), key=lambda r: sum(r.items_per_output))
    timings = [
        (node, int(run.get("executionTime") or 0))
        for node, runs in data["resultData"]["runData"].items()
        for run in runs
    ]
    return exec_id, sum(busiest.items_per_output), busiest.node, timings


def wait_for(conn: psycopg.Connection, batch_id: str, statuses: set[str], timeout: float) -> str:
    """Poll the batch status until it reaches one of ``statuses`` (or 'failed')."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT status FROM ingestion_batches WHERE batch_id = %s", (batch_id,)
        ).fetchone()
        if row and row[0] in statuses | {"failed"}:
            return str(row[0])
        time.sleep(0.25)
    return "timeout"


def main(argv: list[str] | None = None) -> int:
    """Load, trigger, wait, verify, report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path)
    source.add_argument("--n", type=int)
    parser.add_argument("--profile", choices=["real", "wide"], default="real")
    parser.add_argument("--seed", type=int, help="default 42 (--load-only: new users each time)")
    parser.add_argument("--database", help="default: APP_DB_NAME (the database n8n points at)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--url", help="send through this base URL (e.g. the tunnel)")
    target.add_argument(
        "--editor", action="store_true", help="send to the test URL (click Execute workflow first)"
    )
    target.add_argument(
        "--load-only", action="store_true", help="load the batch and stop: no webhook, no checks"
    )
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)

    settings = load_settings()
    database = args.database or settings.get("APP_DB_NAME", "loans")
    if args.csv:
        with args.csv.open(newline="", encoding="utf-8-sig") as handle:
            users = list(csv.DictReader(handle))
        label = args.csv.stem
    else:
        # A fixed seed makes checks repeatable; a loaded demo batch should be new users, not a
        # re-upload that moves an earlier batch's users (the last upload of a user wins).
        seed = args.seed if args.seed is not None else (int(time.time()) if args.load_only else 42)
        users = generate(args.n, seed, args.profile)
        label = f"{args.profile}-{args.n}"

    with benchdb.connect(database) as conn:
        loaded = time.perf_counter()
        batch_id = benchdb.load_batch(conn, users, f"e2e-{label}-{int(time.time())}")
        elapsed = time.perf_counter() - loaded
        print(f"loaded {len(users):,} users as batch {batch_id} in {elapsed:.2f}s")
        if args.load_only:
            print("status 'ingested'. In n8n, open 'B · User-loan matching', pick 'Run manually' "
                  "from the Execute workflow chevron, and click it.")  # fmt: skip
            return 0

        sent = time.perf_counter()
        status = send_webhook(batch_id, settings, args.url, args.editor)
        print(f"webhook: HTTP {status}" + (" (editor test URL)" if args.editor else ""))
        if status == 404 and args.editor:
            print(
                "n8n isn't listening on the test URL. Open 'B · User-loan matching', click "
                "'Execute workflow', then run this within 2 minutes (one call per click). "
                "The batch just loaded stays 'ingested'; the sweeper matches it later."
            )
        if status != 202:
            return 1
        final = wait_for(conn, batch_id, {"matched", "notifying", "notified"}, args.timeout)
        wall = time.perf_counter() - sent
        print(f"batch status: {final} after {wall:.2f}s (webhook to matched)")
        if final in {"matched", "notifying"}:
            # Workflow B calls Workflow C right after marking the batch matched.
            final = wait_for(conn, batch_id, {"notified"}, 120)
            print(f"batch status: {final} after {time.perf_counter() - sent:.2f}s "
                  "(webhook to notified, Workflow C)")  # fmt: skip
        if final == "failed":
            row = conn.execute(
                "SELECT error FROM ingestion_batches WHERE batch_id = %s", (batch_id,)
            ).fetchone()
            print(f"error: {row[0] if row else '?'}")
            return 1
        if final == "timeout":
            return 1
        sent_rows = conn.execute(
            "SELECT status, count(*), array_agg(recipient) FILTER (WHERE recipient IS NOT NULL) "
            "FROM notifications WHERE batch_id = %s GROUP BY status ORDER BY status",
            (batch_id,),
        ).fetchall()
        parts = [
            f"{status} {count:,}" + (f" to {recipients}" if recipients else "")
            for status, count, recipients in sent_rows
        ]
        print(f"notifications: {', '.join(parts) or 'none'}")

        stages = conn.execute(
            "SELECT stage, pairs_in, pairs_out, llm_calls, tokens_in, tokens_out, duration_ms "
            "FROM pipeline_metrics WHERE batch_id = %s ORDER BY metric_id",
            (batch_id,),
        ).fetchall()
        header = ("stage", "pairs_in", "pairs_out", "llm", "tok_in", "tok_out", "ms")
        print("\n  {:15} {:>10} {:>10} {:>4} {:>7} {:>7} {:>7}".format(*header))
        for stage, p_in, p_out, calls, t_in, t_out, ms in stages:
            print(f"  {stage:15} {p_in or 0:>10,} {p_out or 0:>10,} {calls:>4} {t_in:>7} "
                  f"{t_out:>7} {ms or '':>7}")  # fmt: skip

        exec_id, max_items, busiest, timings = execution_profile(
            "manual" if args.editor else "webhook"
        )
        top = sorted(timings, key=lambda x: -x[1])[:3]
        slowest = ", ".join(f"{node} {ms} ms" for node, ms in top)
        print(f"\nn8n execution {exec_id}: at most {max_items} item(s) in one node ({busiest}); "
              f"slowest nodes: {slowest}")  # fmt: skip

        statuses: dict[str, int] = dict(conn.execute(
            "SELECT status, count(*) FROM matches WHERE batch_id = %s GROUP BY status", (batch_id,)
        ).fetchall())  # fmt: skip
        print(f"\nmatches by status: {statuses}")
        ref = reference.evaluate(conn, batch_id)
        got = {(str(u), int(p)) for u, p in conn.execute(
            "SELECT user_id, product_id FROM matches WHERE batch_id = %s", (batch_id,)
        ).fetchall()}  # fmt: skip
        score = reference.score(ref, got)
        print(f"vs brute-force reference: {score.reference:,} pairs, recall {score.recall:.4f}, "
              f"precision {score.precision:.4f}")  # fmt: skip
        return 0 if score.recall == 1.0 and score.precision == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
