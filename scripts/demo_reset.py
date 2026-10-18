"""Put the system where each workflow takes its full path on its next run (`make demo-reset`).

Why: every workflow guards its expensive work, so a repeat run takes the short path by design.
Workflow A skips a page whose cleaned-text hash is unchanged (no LLM call), the warm step finds
every cohort already judged, B's "Run manually" finds no batch waiting, and C finds everyone
already recorded. That is correct, but a demo has to show the long paths too. This script
undoes exactly those guards and nothing else:

  default (run before Workflow A):
    * forget one source's page hash (--source, default hdfc-bank). A sends that page to the
      LLM, validates and upserts its products; the other sources still show the unchanged skip.
    * drop the cached verdicts and compiled rules of the cohorts the active products can
      produce. A's warm step judges them again through the LLM gateway.
  --cold-batch N (run before B's "Run manually"):
    * drop the same cache rows and load N new users as a waiting ('ingested') batch. B claims
      it, judges its cohorts itself, writes the matches and calls C. C then emails the test
      aliases (email_mode test_aliases) or only records (suppressed).

Users, products, matches and notifications are never deleted. The dropped cache rows are
rebuilt by the next run: about 2 Gemini requests, plus 1 per forgotten page.

Usage:
    uv run python scripts/demo_reset.py                       # before A, on the local app-db
    uv run python scripts/demo_reset.py --cold-batch 1000     # before B's "Run manually"
    uv run python scripts/demo_reset.py --target rds          # the AWS demo database
    uv run python scripts/demo_reset.py --dry-run             # print the plan, change nothing
"""

import argparse
import sys
import time
from dataclasses import dataclass, field

import psycopg

from bench import benchdb
from scripts.dbconn import Target, admin_connect_kwargs
from scripts.envfile import load_settings
from scripts.gen_users import generate

# The cohorts the warm step would judge: the same space and banding rule as warm_cohorts.sql.
SPACE_SQL = "SELECT DISTINCT criterion_key, signature FROM cohort_space(%s)"
DROP_VERDICTS_SQL = """
DELETE FROM llm_decisions d
USING (SELECT DISTINCT criterion_key, signature FROM cohort_space(%s)) s
WHERE d.criteria_hash = s.criterion_key AND d.user_signature = s.signature
"""
DROP_RULES_SQL = """
DELETE FROM criterion_rules
WHERE criterion_key IN (SELECT criterion_key FROM cohort_space(%s))
"""
# What B's claim step will find uncached for a batch (uncached_cohorts.sql uses the same function).
BATCH_COHORTS_SQL = (
    "SELECT count(DISTINCT (criterion_key, signature)) FROM fuzzy_signatures(%s::uuid, %s)"
)
LLM_TODAY_SQL = """
SELECT count(*) FROM llm_calls
WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'America/Los_Angeles')
                     AT TIME ZONE 'America/Los_Angeles')
"""


@dataclass
class Plan:
    """What the reset found and what it will change, printed before anything is changed."""

    config: dict[str, str]
    sources: list[str]
    unknown_sources: list[str]
    cohorts: int
    criteria: int
    waiting: list[tuple[str, str]]
    llm_today: int
    notes: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        """Banding mode as warm_cohorts.sql reads it: anything unknown means threshold."""
        bands = self.config.get("cohort_bands", "threshold")
        return bands if bands in ("coarse", "fine", "exact") else "threshold"


def read_plan(conn: psycopg.Connection, source: str) -> Plan:
    """Look at the database without changing it."""
    config: dict[str, str] = dict(conn.execute("SELECT key, value FROM app_config").fetchall())
    enabled = [r[0] for r in conn.execute(
        "SELECT slug FROM crawl_sources WHERE enabled ORDER BY source_id").fetchall()]  # fmt: skip
    sources: list[str] = []
    unknown: list[str] = []
    if source == "all":
        sources = enabled
    elif source != "none":
        wanted = [s.strip() for s in source.split(",") if s.strip()]
        sources = [s for s in wanted if s in enabled]
        unknown = [s for s in wanted if s not in enabled]
    plan = Plan(config, sources, unknown, 0, 0, [], 0)
    space = conn.execute(SPACE_SQL, (plan.mode,)).fetchall()
    plan.cohorts = len(space)
    plan.criteria = len({key for key, _ in space})
    plan.waiting = [(r[0], r[1]) for r in conn.execute(
        "SELECT batch_id::text, status FROM ingestion_batches "
        "WHERE status IN ('ingested', 'matching') ORDER BY received_at").fetchall()]  # fmt: skip
    row = conn.execute(LLM_TODAY_SQL).fetchone()
    plan.llm_today = int(row[0]) if row else 0

    if config.get("match_llm_enabled", "true") != "true":
        plan.notes.append("match_llm_enabled is false: the warm step and B skip the LLM branch")
    if plan.cohorts == 0:
        plan.notes.append("no active product has a fuzzy criterion: there is nothing to judge")
    if config.get("crawl_mode") != "live":
        plan.notes.append("crawl_mode is fixtures: A fetches the saved pages through the same "
                          "nodes, and skips the polite pause meant for live sites")  # fmt: skip
    budget = int(config.get("llm_rpd", "500"))
    if plan.llm_today + len(sources) + 4 > budget:
        plan.notes.append(f"only {budget - plan.llm_today} LLM requests left today")
    return plan


def describe(plan: Plan, cold_batch: int | None) -> str:
    """The plan in words: what each workflow's next run will do."""
    email = plan.config.get("email_mode", "?")
    lines = []
    if plan.sources:
        others = "the other sources show the unchanged-page skip"
        lines.append(f"A: re-extracts {', '.join(plan.sources)} with the LLM; {others}")
    else:
        lines.append("A: every page hash is kept, so every source takes the unchanged skip")
    lines.append(f"cache: drop verdicts for {plan.cohorts} cohort(s) and rules for "
                 f"{plan.criteria} criterion(s) the active products can produce")  # fmt: skip
    if cold_batch:
        lines.append(f"B: load {cold_batch:,} new users as a waiting batch; 'Run manually' "
                     "claims it and judges its cohorts through the LLM gateway")  # fmt: skip
    else:
        lines.append("then A's warm step judges them, and the next upload finds them cached")
    sends = "emails up to email_cap test aliases" if email == "test_aliases" else "sends nothing"
    lines.append(f"C: email_mode is {email}, so C {sends} for each new batch")
    for batch_id, status in plan.waiting:
        lines.append(f"note: batch {batch_id} is already '{status}'; B will pick it up too")
    lines.extend(f"note: {n}" for n in plan.notes)
    lines.extend(f"unknown or disabled source: {s}" for s in plan.unknown_sources)
    return "\n".join(f"  {line}" for line in lines)


def apply(conn: psycopg.Connection, plan: Plan, cold_batch: int | None) -> str | None:
    """Make the changes in one transaction; the cold batch's id if one was loaded."""
    batch_id = None
    with conn.transaction():
        if plan.sources:
            conn.execute(
                "UPDATE crawl_sources SET last_content_hash = NULL WHERE slug = ANY(%s)",
                (plan.sources,),
            )
        verdicts = conn.execute(DROP_VERDICTS_SQL, (plan.mode,)).rowcount
        rules = conn.execute(DROP_RULES_SQL, (plan.mode,)).rowcount
        print(f"dropped {verdicts} cached verdict(s) and {rules} compiled rule(s)")
        if cold_batch:
            users = generate(cold_batch, int(time.time()), "real")
            batch_id = benchdb.load_batch(conn, users, f"demo-cold-{int(time.time())}")
            row = conn.execute(BATCH_COHORTS_SQL, (batch_id, plan.mode)).fetchone()
            print(f"loaded batch {batch_id}: {cold_batch:,} users, "
                  f"{row[0] if row else 0} cohort(s) for B to judge")  # fmt: skip
    return batch_id


def main(argv: list[str] | None = None) -> int:
    """Print the plan, confirm, apply."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--target", choices=["local", "rds"], default="local")
    parser.add_argument("--database", help="a local database other than APP_DB_NAME (checks)")
    parser.add_argument(
        "--source",
        help="source slug(s) A re-extracts, comma-separated, or 'all' / 'none' "
        "(default: hdfc-bank, or none with --cold-batch)",
    )
    parser.add_argument("--cold-batch", type=int, metavar="N", help="load N users for B")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args(argv)
    if args.database and args.target != "local":
        parser.error("--database is for local databases only")
    if args.cold_batch is not None and args.cold_batch < 1:
        parser.error("--cold-batch needs at least 1 user")
    target: Target = args.target
    # hdfc-bank: the smallest page (one product, about 4 KB of text) and the one whose product
    # carries the fuzzy criterion. --cold-batch is for B, so A's hashes stay unless named.
    source = args.source or ("none" if args.cold_batch else "hdfc-bank")

    kwargs = admin_connect_kwargs(load_settings(), target, args.database)
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        plan = read_plan(conn, source)
        print(f"demo reset on {target} database '{kwargs['dbname']}':")
        print(describe(plan, args.cold_batch))
        if plan.unknown_sources:
            return 1
        if args.dry_run:
            return 0
        if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
            print("nothing changed")
            return 1
        batch_id = apply(conn, plan, args.cold_batch)

    if batch_id:
        print("next: in n8n open 'B · User-loan matching', pick 'Run manually' from the "
              "Execute workflow chevron, and click it")  # fmt: skip
    else:
        print("next: in n8n open 'A · Loan product discovery' and click Execute workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
