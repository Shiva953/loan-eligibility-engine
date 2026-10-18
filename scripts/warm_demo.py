"""Show the warm step doing real work, then a batch that makes no LLM call (`make warm-demo`).

Why a scratch database: in `loans` everything is already cached, so warming has nothing to do.
This rebuilds `loans_bench` with the synthetic products (19 cohorts, 12 fuzzy criteria, nothing
cached), points n8n's app Postgres credential at it, runs the "Warm match cache" sub-workflow
through n8n (2 Gemini calls), then sends a 10k batch through Workflow B, which should find
everything cached. The credential is always pointed back at the app database afterwards, and
the demo's LLM requests are copied into the app database's ledger so the daily budget stays
exact.

--lazy skips the warm step, so Workflow B finds nothing cached and makes the 2 requests itself:
the path a batch takes when warming missed something (or the crawl's warm step failed).

Usage:
    uv run python scripts/warm_demo.py
    uv run python scripts/warm_demo.py --lazy
"""

import argparse
import os
import subprocess
import sys

from bench import benchdb
from scripts import match_e2e
from scripts.envfile import REPO_ROOT, load_settings
from scripts.n8n_ids import WORKFLOWS

ONE_OFF = [
    "docker", "compose", "run", "--rm", "--no-deps", "-T",
    "-e", "N8N_RUNNERS_BROKER_PORT=5690",
    "-e", "N8N_RUNNERS_MODE=internal",
    "-e", "NODE_FUNCTION_ALLOW_BUILTIN=crypto",
    "n8n",
]  # fmt: skip
LEDGER_COLUMNS = (
    "execution_id, purpose, provider, model, attempt, http_status, outcome, tokens_in, "
    "tokens_out, duration_ms, error, created_at"
)


def point_credential_at(database: str) -> None:
    """Re-import n8n's credentials with the app Postgres credential on ``database``."""
    subprocess.run(
        ["./n8n/import-credentials.sh", "local"],
        env={**os.environ, "APP_DB_NAME": database}, cwd=REPO_ROOT, check=True,
        stdout=subprocess.DEVNULL,
    )  # fmt: skip


def calls_today(database: str) -> int:
    """Requests counted against today's quota in one database's ledger."""
    sql_text = (REPO_ROOT / "sql" / "llm" / "config_and_usage.sql").read_text(encoding="utf-8")
    with benchdb.connect(database) as conn:
        row = conn.execute(sql_text.encode()).fetchone()
    return int(row[1]) if row else 0


def main(argv: list[str] | None = None) -> int:
    """Warm the scratch database through n8n, then match a batch against it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--lazy", action="store_true", help="skip warming: B judges the cohorts")
    args = parser.parse_args(argv)
    settings = load_settings()
    app_db = settings.get("APP_DB_NAME", "loans")
    budget = int(settings.get("LLM_RPD", "500"))
    if calls_today(app_db) + 2 > budget:
        raise SystemExit("today's LLM budget can't cover the demo's 2 requests")

    benchdb.recreate("synthetic")
    point_credential_at(benchdb.BENCH_DB)
    try:
        if args.lazy:
            print("not warming: Workflow B will judge the cohorts itself", flush=True)
        else:
            print("warming through n8n ...", flush=True)
            result = subprocess.run(
                [*ONE_OFF, "execute", f"--id={WORKFLOWS['warm_match_cache']}"],
                capture_output=True, text=True, check=False, cwd=REPO_ROOT,
            )  # fmt: skip
            if result.returncode != 0 or '"status": "success"' not in result.stdout:
                print((result.stdout + result.stderr)[-2000:], file=sys.stderr)
                return 1
        with benchdb.connect() as conn:
            for stage, cohorts, uncached, calls, details in conn.execute(
                "SELECT stage, pairs_in, pairs_out, llm_calls, details FROM pipeline_metrics "
                "ORDER BY metric_id"
            ).fetchall():
                print(f"  {stage:10} cohorts {cohorts}  uncached/judged {uncached}  "
                      f"llm calls {calls}  {details}")  # fmt: skip
        state = "unwarmed" if args.lazy else "warmed"
        print(f"\na 10k batch through Workflow B on the {state} database:", flush=True)
        status = match_e2e.main(["--database", benchdb.BENCH_DB, "--n", "10000"])
        with benchdb.connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pipeline_metrics "
                "WHERE stage = 's5_llm' AND batch_id IS NOT NULL"
            ).fetchone()
        print(f"\nLLM stages run at match time: {row[0] if row else '?'} (0 = no LLM call)")
        with benchdb.connect() as conn:
            for purpose, calls, ms, out in conn.execute(
                "SELECT purpose, count(*), max(duration_ms), max(tokens_out) FROM llm_calls "
                "GROUP BY 1 ORDER BY 1"
            ).fetchall():
                print(f"  gateway {purpose:16} {calls} call(s), {ms} ms, {out} tokens out")
        return status
    finally:
        point_credential_at(app_db)
        with benchdb.connect() as bench, benchdb.connect(app_db) as app, app.cursor() as cur:
            rows = bench.execute(f"SELECT {LEDGER_COLUMNS} FROM llm_calls").fetchall()
            cur.executemany(
                f"INSERT INTO llm_calls ({LEDGER_COLUMNS}) VALUES ({', '.join(['%s'] * 12)})",
                rows,
            )
        print(f"n8n points at {app_db} again; {len(rows)} LLM request(s) added to its ledger")


if __name__ == "__main__":
    sys.exit(main())
