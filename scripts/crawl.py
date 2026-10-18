"""Run Workflow A once from the command line and report what it did.

Why a one-off container: ``n8n execute`` can't run inside the live n8n container, because the
task-broker port is taken (docs/DECISIONS.md V15). The one-off container shares the database,
credentials and network, and runs Code nodes with an internal task runner, given the same
``crypto`` allowance as the external one.

The report is read from the database (crawl_runs, loan_products, llm_calls), not from the
CLI's output, so it shows what was actually stored.

Usage:
    uv run python scripts/crawl.py            # run A, print the report
    uv run python scripts/crawl.py --reset    # forget page hashes first, forcing re-extraction
    uv run python scripts/crawl.py --reset bankbazaar   # re-extract one source only
    uv run python scripts/crawl.py --report   # report on the latest run only
"""

import argparse
import subprocess
import sys

import psycopg

from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import load_settings
from scripts.n8n_ids import WORKFLOWS

ONE_OFF = [
    "docker", "compose", "run", "--rm", "--no-deps", "-T",
    "-e", "N8N_RUNNERS_BROKER_PORT=5690",
    "-e", "N8N_RUNNERS_MODE=internal",
    "-e", "NODE_FUNCTION_ALLOW_BUILTIN=crypto",
    "n8n",
]  # fmt: skip

RUNS_SQL = """
SELECT s.slug, r.status, r.http_status, r.products_found, r.llm_calls, r.tokens_in,
       r.tokens_out, left(coalesce(r.error, ''), 90)
FROM crawl_runs r JOIN crawl_sources s USING (source_id)
WHERE r.execution_id = (SELECT execution_id FROM crawl_runs ORDER BY run_id DESC LIMIT 1)
ORDER BY s.source_id
"""
PRODUCTS_SQL = """
SELECT s.slug,
       count(*) FILTER (WHERE p.is_active)                                    AS active,
       count(*) FILTER (WHERE p.is_active AND p.min_monthly_income IS NOT NULL) AS with_income,
       count(*) FILTER (WHERE p.is_active AND p.interest_rate_min IS NOT NULL)  AS with_rate,
       count(*) FILTER (WHERE p.is_active AND p.has_fuzzy_criteria)             AS fuzzy,
       count(*) FILTER (WHERE p.is_active AND p.needs_review)                   AS needs_review
FROM crawl_sources s LEFT JOIN loan_products p USING (source_id)
WHERE s.enabled
GROUP BY s.slug, s.source_id
ORDER BY s.source_id
"""
LLM_SQL = """
SELECT count(*) FILTER (WHERE outcome = 'ok'), count(*) FILTER (WHERE outcome = 'rate_limited'),
       count(*) FILTER (WHERE outcome = 'error'), coalesce(sum(tokens_in), 0),
       coalesce(sum(tokens_out), 0)
FROM llm_calls
WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'America/Los_Angeles')
                     AT TIME ZONE 'America/Los_Angeles')
"""


def report() -> None:
    """Print the latest execution's crawl runs, the product counts and today's LLM usage."""
    with psycopg.connect(**admin_connect_kwargs(load_settings(), "local")) as conn:
        runs = conn.execute(RUNS_SQL).fetchall()
        products = conn.execute(PRODUCTS_SQL).fetchall()
        llm = conn.execute(LLM_SQL).fetchone()

    print("\nLatest crawl, per source:")
    print(f"  {'source':14} {'status':10} {'http':>4} {'products':>8} {'llm':>4} "
          f"{'tok_in':>7} {'tok_out':>7}  error")  # fmt: skip
    for slug, status, http, found, calls, t_in, t_out, err in runs:
        print(f"  {slug:14} {status:10} {http or '':>4} {found if found is not None else '':>8} "
              f"{calls:>4} {t_in:>7} {t_out:>7}  {err}")  # fmt: skip
    print("\nActive products per source:")
    print(f"  {'source':14} {'active':>6} {'income':>6} {'rate':>5} {'fuzzy':>5} {'review':>6}")
    for slug, active, income, rate, fuzzy, review in products:
        print(f"  {slug:14} {active:>6} {income:>6} {rate:>5} {fuzzy:>5} {review:>6}")
    if llm:
        ok, limited, errors, t_in, t_out = llm
        print(f"\nLLM calls today (Pacific day): {ok} ok, {limited} rate-limited, {errors} errors; "
              f"{t_in} tokens in, {t_out} out")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    """Optionally reset hashes, run Workflow A, then report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--reset",
        nargs="?",
        const="*",
        metavar="SLUG",
        help="force re-extraction of every source, or of one source by slug",
    )
    parser.add_argument("--report", action="store_true", help="only print the latest report")
    args = parser.parse_args(argv)

    if not args.report:
        if args.reset:
            with psycopg.connect(**admin_connect_kwargs(load_settings(), "local")) as conn:
                conn.execute(
                    "UPDATE crawl_sources SET last_content_hash = NULL WHERE %s IN ('*', slug)",
                    (args.reset,),
                )
            print(f"page hash cleared for {'every source' if args.reset == '*' else args.reset}")
        command = [*ONE_OFF, "execute", f"--id={WORKFLOWS['a_product_discovery']}"]
        print("running Workflow A ...", flush=True)
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or "Execution was NOT successful" in result.stdout:
            tail = (result.stdout + result.stderr).strip().splitlines()[-25:]
            print("Workflow A failed:\n  " + "\n  ".join(tail), file=sys.stderr)
            report()
            return 1
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
