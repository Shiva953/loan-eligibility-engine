"""The optimisation bench (`make bench`): Workflow B's reduction funnel, measured and verified.

For each product set x user profile x batch size it rebuilds an empty pipeline in the bench
database, loads one batch and runs Workflow B's own SQL files (scripts.n8n_sql, bound the way
n8n binds them):

    S0 naive         every (user, product) pair goes to the LLM: N x M calls
    S2 hard filter   pairs that pass the hard rules (match_candidates, the SQL join)
    S3 cohorts       pending (pair, fuzzy criterion) rows collapse to (criterion, banded facts)
    S4 cache         cohorts already judged are not asked again
    S5 batching      K cohorts per call: calls = ceil(uncached / K)
    rerun            the same batch again: every cohort is cached, 0 calls
    warm             the calls Workflow A's warm step makes before any batch, and the calls a
                     batch still makes after it (the cohorts it produces outside cohort_space)

The LLM itself is not called here (its quality is bench/llm_experiments.py's job): the calls a
run would make are counted, and every uncached cohort gets a simulated 'uncertain' verdict so
the write produces realistic statuses. Every run checks its pairs against the brute-force
reference (bench/reference.py): recall and precision must both be 1.0.

The deterministic stage is also timed four ways on the same users and products:
    d0  JavaScript nested loop over users x products, emitting one item per pair: what the
        matching becomes if the users are pulled into an n8n Code node
    d4  JavaScript bitmasks over the products' cut points (binary search per field)
    d1  the SQL join that production uses (match_candidates)
    d2  SQL decision cells: each occupied cell of the cut-point grid is tested once

Usage:
    uv run python bench/run.py                       # real + synthetic products, 1k/10k/100k
    uv run python bench/run.py --sizes 1000,10000 --profiles real --products real
Results: bench/results/<products>-<profile>-<n>.json, plus the printed table.
"""

import argparse
import json
import math
import subprocess
import sys
import time
from typing import Any, cast

import psycopg

from bench import benchdb, reference
from bench.benchdb import ProductSet
from scripts.envfile import REPO_ROOT
from scripts.gen_users import Profile, generate
from scripts.n8n_sql import load, params, run, run_dicts

RESULTS = REPO_ROOT / "bench" / "results"
WORK = REPO_ROOT / "build" / "bench"
MODES = ("threshold", "coarse", "fine", "exact")


def llm_calls(found: dict[str, Any], k: int) -> int:
    """Gateway calls a run needs: K cohorts per judging call, 25 criteria per compile call."""
    return math.ceil(int(found["cohorts_uncached"]) / k) + math.ceil(
        int(found["criteria_uncompiled"]) / 25
    )


def cohorts_by_mode(conn: psycopg.Connection, batch_id: str) -> dict[str, int]:
    """Distinct (criterion, signature) cohorts the batch has under each banding mode."""
    out = {}
    for mode in MODES:
        row = conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT criterion_key, signature "
            "FROM fuzzy_signatures(%s, %s)) c",
            (batch_id, mode),
        ).fetchone()
        out[mode] = int(row[0]) if row else 0
    return out


def simulate_verdicts(
    conn: psycopg.Connection,
    batch_id: str,
    cohorts: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
) -> None:
    """Cache what the LLM would have answered, without calling it.

    Every cohort gets 'uncertain', which keeps its pairs. Every criterion gets a rule with one
    clause that covers nobody (age 999+), so the write still pays for applying the rules.
    """
    decisions = [
        {
            "criterion_key": c["criterion_key"],
            "signature": c["signature"],
            "verdict": "uncertain",
            "reason": "bench: simulated verdict",
            "model": "bench-simulated",
            "prompt_version": "bench",
        }
        for c in cohorts
    ]
    never = {
        "verdict": "ineligible",
        "employment_status": [],
        "age_min": 999,
        "age_max": None,
        "credit_score_min": None,
        "credit_score_max": None,
        "monthly_income_min": None,
        "monthly_income_max": None,
    }
    rules = [
        {
            **c,
            "clauses": [never],
            "note": "bench: simulated rule",
            "model": "bench-simulated",
            "prompt_version": "bench",
        }
        for c in criteria
    ]
    stats = {
        "calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "requests": 0,
        "failed_requests": 0,
        "unanswered": 0,
    }
    run(
        conn,
        "match/store_decisions.sql",
        json.dumps(decisions),
        batch_id,
        "bench",
        json.dumps(stats),
        json.dumps(rules),
    )


# --- the deterministic stage, four ways ----------------------------------------------------------


def js_variant(
    variant: str, users: list[dict[str, Any]], products: list[reference.Product]
) -> dict[str, Any]:
    """Run d0 or d4 in Node on the batch; return its timing and pair set."""
    WORK.mkdir(parents=True, exist_ok=True)
    payload = WORK / "s2_input.json"
    pairs_file = WORK / f"s2_{variant}.txt"
    payload.write_text(
        json.dumps(
            {
                "users": users,
                "products": [
                    {
                        "product_id": p.product_id,
                        "min_monthly_income": p.min_monthly_income,
                        "min_credit_score": p.min_credit_score,
                        "min_age": p.min_age,
                        "max_age": p.max_age,
                        "allowed_employment": None
                        if p.allowed_employment is None
                        else list(p.allowed_employment),
                        "filter_rules": [[*r[:3], list(r[3])] for r in p.filter_rules],
                        "has_fuzzy": p.has_fuzzy,
                    }
                    for p in products
                ],
            }
        ),
        encoding="utf-8",
    )
    out = subprocess.run(
        [
            "node",
            "--expose-gc",
            "bench/variants/js_matchers.mjs",
            variant,
            str(payload),
            str(pairs_file),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    stats: dict[str, Any] = json.loads(out.stdout)
    text = pairs_file.read_text(encoding="utf-8")
    stats["pairs_set"] = {
        (u, int(p)) for u, p in (line.split(",") for line in text.splitlines() if line)
    }
    return stats


def sql_variant(conn: psycopg.Connection, query: str, batch_id: str) -> dict[str, Any]:
    """Time a SQL variant as a count (no client transfer), then fetch its pairs for checking."""
    started = time.perf_counter()
    row = conn.execute(f"SELECT count(*) FROM ({query}) q", params(batch_id)).fetchone()
    ms = (time.perf_counter() - started) * 1000
    rows = conn.execute(f"SELECT user_id, product_id FROM ({query}) q", params(batch_id))
    pairs = {(str(u), int(p)) for u, p in rows}
    return {"ms": ms, "pairs": int(row[0]) if row else 0, "pairs_set": pairs}


def s2_variants(
    conn: psycopg.Connection, batch_id: str, ref: set[tuple[str, int]]
) -> dict[str, Any]:
    """d0 / d4 / d1 / d2 on the same batch, each checked for exactness against the reference."""
    users = [
        {
            "user_id": u,
            "monthly_income": float(inc),
            "credit_score": cs,
            "employment_status": emp,
            "age": age,
        }
        for u, inc, cs, emp, age in conn.execute(
            "SELECT user_id::text, monthly_income, credit_score, employment_status, age "
            "FROM users WHERE batch_id = %s",
            (batch_id,),
        )
    ]
    products = reference.load_products(conn)
    d1 = "SELECT user_id, product_id FROM match_candidates(%(p1)s)"
    d2 = load("../bench/variants/d2_cells.sql")
    out = {
        "d0_js_naive": js_variant("d0", users, products),
        "d4_js_bitmask": js_variant("d4", users, products),
        "d1_sql_join": sql_variant(conn, d1, batch_id),
        "d2_sql_cells": sql_variant(conn, d2, batch_id),
    }
    for stats in out.values():
        stats["exact"] = stats.pop("pairs_set") == ref
    return out


# --- one run --------------------------------------------------------------------------------------


def run_once(
    conn: psycopg.Connection, products: ProductSet, profile: Profile, n: int, *, variants: bool
) -> dict[str, Any]:
    """Load one batch into an empty pipeline and measure the funnel, the write and a rerun."""
    benchdb.clear_batches(conn)
    config: dict[str, str] = dict(conn.execute("SELECT key, value FROM app_config").fetchall())
    mode, k = config["cohort_bands"], int(config["llm_batch_k"])
    started = time.perf_counter()
    batch_id = benchdb.load_batch(conn, generate(n, 42, profile), f"bench-{products}-{profile}-{n}")
    load_ms = (time.perf_counter() - started) * 1000

    rules = config["match_rules_enabled"]
    # Warming (Workflow A, after the crawl): what the products alone need judged, before any
    # batch. Counted here, not stored, so the batch below still measures the lazy path.
    warm = run_dicts(conn, "match/warm_cohorts.sql", mode, "true", rules, "bench")[0]
    started = time.perf_counter()
    first = run_dicts(
        conn, "match/uncached_cohorts.sql", batch_id, mode, "true", "bench", rules, "false"
    )[0]
    uncached_ms = (time.perf_counter() - started) * 1000
    simulate_verdicts(conn, batch_id, first["cohorts"], first["criteria"])
    started = time.perf_counter()
    written = run_dicts(conn, "match/write_matches.sql", batch_id, mode, "true", "bench", rules)[0]
    write_ms = (time.perf_counter() - started) * 1000
    batch_users, active = written["batch_users"], written["active_products"]

    # Everything the batch needed is cached now; whatever the warm set still lacks is what
    # warming would have judged in vain (0 under threshold banding: the space is exactly the
    # cohorts batches produce).
    warm_left = run_dicts(conn, "match/warm_cohorts.sql", mode, "true", rules, "bench")[0]
    # The batch's cohorts that warming would not have covered: after warming, the batch asks
    # the LLM only about these (0 by construction; measured, not assumed).
    gap_row = conn.execute(
        "SELECT count(*) FROM (SELECT criterion_key, signature FROM fuzzy_signatures(%s, %s) "
        "EXCEPT SELECT criterion_key, signature FROM cohort_space(%s)) g",
        (batch_id, mode, mode),
    ).fetchone()
    warm_gap = int(gap_row[0]) if gap_row else 0
    started = time.perf_counter()
    again = run_dicts(
        conn, "match/uncached_cohorts.sql", batch_id, mode, "true", "bench", rules, "false"
    )[0]
    rerun_uncached_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    rewritten = run_dicts(conn, "match/write_matches.sql", batch_id, mode, "true", "bench", rules)[
        0
    ]
    rerun_write_ms = (time.perf_counter() - started) * 1000

    ref_started = time.perf_counter()
    ref = reference.evaluate(conn, batch_id)
    ref_ms = (time.perf_counter() - ref_started) * 1000
    got = {
        (str(u), int(p))
        for u, p in conn.execute(
            "SELECT user_id, product_id FROM matches WHERE batch_id = %s", (batch_id,)
        )
    }
    score = reference.score(ref, got)
    pairs_with_fuzzy = sum(1 for s in ref.values() if s == "pending_llm")

    result: dict[str, Any] = {
        "products": products,
        "profile": profile,
        "users": batch_users,
        "active_products": active,
        "banding": mode,
        "k": k,
        "funnel": {
            "s0_naive_llm_calls": batch_users * active,
            "s2_pairs_after_hard_filter": written["pairs"],
            "pairs_with_fuzzy_criteria": pairs_with_fuzzy,
            "s3_pending_questions": int(first["pending_rows"]),
            "s3_cohorts": int(first["cohorts_total"]),
            "s4_uncached_cohorts": int(first["cohorts_uncached"]),
            "s4_uncompiled_criteria": int(first["criteria_uncompiled"]),
            "s5_llm_calls": llm_calls(first, k),
            "rerun_llm_calls": llm_calls(again, k),
            "warm_llm_calls": llm_calls(warm, k),
            "warm_cohorts": int(warm["cohorts_total"]),
            "warm_cohorts_no_batch_needed": int(warm_left["cohorts_uncached"]),
            "match_time_calls_after_warming": math.ceil(warm_gap / k),
        },
        "statuses": {
            k: written[k] for k in ("matched", "llm_matched", "llm_rejected", "pending_llm")
        },
        "rows_written": written["rows_written"],
        "rerun_rows_written": rewritten["rows_written"],
        "cohorts_by_mode": cohorts_by_mode(conn, batch_id),
        "max_items_in_a_node": max(1, llm_calls(first, k)),
        "timings_ms": {
            "load_users": round(load_ms),
            "s3_s4_uncached_cohorts": round(uncached_ms),
            "write_matches": round(write_ms),
            "rerun_uncached_cohorts": round(rerun_uncached_ms),
            "rerun_write_matches": round(rerun_write_ms),
            "reference_python": round(ref_ms),
        },
        "recall": score.recall,
        "precision": score.precision,
    }
    if variants:
        result["s2_variants"] = s2_variants(conn, batch_id, set(ref))
    return result


def print_table(results: list[dict[str, Any]]) -> None:
    """The funnel and timings, one row per run."""
    # calls: made by the batch with nothing cached; warm: made at crawl time by the warm step;
    # after warm: what a batch still makes once warming ran (0 means B never waits for the LLM).
    head = (
        f"{'products':9} {'profile':7} {'users':>7} {'naive calls':>12} {'after S2':>10} "
        f"{'fuzzy pairs':>11} {'cohorts':>7} {'calls':>5} {'rerun':>5} {'warm':>4} "
        f"{'after warm':>10} {'recall':>6} {'write s':>7} {'rerun s':>7}"
    )
    print(head)
    for r in results:
        f, t = r["funnel"], r["timings_ms"]
        print(
            f"{r['products']:9} {r['profile']:7} {r['users']:>7,} {f['s0_naive_llm_calls']:>12,} "
            f"{f['s2_pairs_after_hard_filter']:>10,} {f['pairs_with_fuzzy_criteria']:>11,} "
            f"{f['s3_cohorts']:>7} {f['s5_llm_calls']:>5} {f['rerun_llm_calls']:>5} "
            f"{f['warm_llm_calls']:>4} {f['match_time_calls_after_warming']:>10} "
            f"{r['recall']:>6.3f} {t['write_matches'] / 1000:>7.2f} "
            f"{t['rerun_write_matches'] / 1000:>7.2f}"
        )
    variants = [r for r in results if "s2_variants" in r]
    if variants:
        print(
            f"\nS2 variants (ms, exact vs reference): {'':4}"
            + "".join(f"{name:>18}" for name in variants[0]["s2_variants"])
        )
        for r in variants:
            cells = "".join(
                f"{v['ms']:>12.0f} {'ok' if v['exact'] else 'WRONG':>5}"
                for v in r["s2_variants"].values()
            )
            print(f"  {r['products']:9} {r['profile']:7} {r['users']:>7,}   {cells}")


def main(argv: list[str] | None = None) -> int:
    """Run the bench matrix and write one JSON result per run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--profiles", default="real,wide")
    parser.add_argument("--products", default="real,synthetic")
    parser.add_argument("--no-variants", action="store_true", help="skip the d0/d4/d1/d2 timings")
    args = parser.parse_args(argv)

    RESULTS.mkdir(parents=True, exist_ok=True)
    results = []
    for products in [cast("ProductSet", p) for p in args.products.split(",")]:
        benchdb.recreate(products)
        with benchdb.connect() as conn:
            for profile in [cast("Profile", p) for p in args.profiles.split(",")]:
                for n in map(int, args.sizes.split(",")):
                    print(f"... {products} products, {profile} profile, {n:,} users", flush=True)
                    r = run_once(conn, products, profile, n, variants=not args.no_variants)
                    path = RESULTS / f"{products}-{profile}-{n}.json"
                    path.write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
                    results.append(r)
    print()
    print_table(results)
    failed = [
        r
        for r in results
        if r["recall"] != 1.0
        or r["precision"] != 1.0
        or not all(v["exact"] for v in r.get("s2_variants", {}).values())
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
