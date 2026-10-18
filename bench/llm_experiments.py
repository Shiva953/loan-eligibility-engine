"""LLM-stage experiments: which way of judging fuzzy criteria matches the oracle, at what cost.

Setup: the synthetic product set (15 fuzzy criterion instances, 12 distinct texts) and a 10k
real-profile batch. The oracle asks about one (user, criterion) pair at a time, giving the user's
exact facts. It is the most expensive and most faithful use of the LLM, so every cheaper
strategy is scored by how often it reaches the oracle's decision on the same sampled pairs.

The oracle reuses the production prompt builder (n8n/code/build_cohort_requests.js): an oracle
question is a cohort of one user, with all four facts given exactly and K = 1. Any
disagreement therefore comes from the strategy (banding, grouping, batching), not from
different prompt wording.

Strategies:
    per_pair    S0/L1: one call per (user, criterion). Equal to the oracle by construction;
                its cost is computed, not spent.
    per_user    L2: one call per user, listing all of that user's fuzzy criteria.
    cohort      L4 (S3 + S4 + S5): one question per distinct banded cohort, K per call, with
                banding threshold | coarse | fine | exact, and K in {1, 10, 25, 50}.
    adaptive    coarse first; only cohorts answered "uncertain" are re-asked at fine banding.
    compile     L5: one call turns every fuzzy criterion into explicit conditions (DNF over
                the facts it depends on); afterwards SQL decides. No match-time calls.
    rules_only  E4: fuzzy criteria become caveats and nobody is excluded by them. 0 calls.

Usage:
    uv run python bench/llm_experiments.py            # run (or reuse cached) experiments
    uv run python bench/llm_experiments.py --dry-run  # print the plan and call counts only
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import psycopg

from bench import benchdb
from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import REPO_ROOT, load_settings
from scripts.gen_users import generate

RESULTS = REPO_ROOT / "bench" / "results" / "llm"
WORK = REPO_ROOT / "build" / "bench"
FACTS = ("age", "credit_score", "employment_status", "monthly_income")
BATCH_LABEL = "llm-syn-10k"


# ---------------------------------------------------------------------------------------------
# Plumbing: Code-node files, the LLM runner, the call ledger
# ---------------------------------------------------------------------------------------------
def code_node(
    file: str, items: list[dict[str, Any]], nodes: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Run one n8n Code-node file through the same harness the tests use."""
    WORK.mkdir(parents=True, exist_ok=True)
    payload = WORK / "code_node_input.json"
    payload.write_text(json.dumps({"items": items, "nodes": nodes or {}}), encoding="utf-8")
    out = subprocess.run(
        ["node", "bench/run_code_node.mjs", file, str(payload)],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    result: list[dict[str, Any]] = json.loads(out.stdout)
    return result


def calls_today(settings: dict[str, str]) -> int:
    """Provider requests that count against today's quota (same query the gateway uses)."""
    sql_text = (REPO_ROOT / "sql" / "llm" / "config_and_usage.sql").read_text(encoding="utf-8")
    with psycopg.connect(**admin_connect_kwargs(settings, "local")) as conn:
        row = conn.execute(sql_text.encode()).fetchone()
    return int(row[1]) if row else 0


def run_llm(
    name: str, requests: list[dict[str, Any]], config: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Send requests through bench/llm_runner.mjs; log real calls in the app DB's ledger."""
    settings = load_settings()
    WORK.mkdir(parents=True, exist_ok=True)
    req_file, out_file = WORK / f"{name}.requests.json", WORK / f"{name}.results.jsonl"
    req_file.write_text(json.dumps({"config": config, "requests": requests}), encoding="utf-8")
    # Only uncached requests are sent, so only they count against the daily budget.
    count = subprocess.run(
        ["node", "bench/llm_runner.mjs", str(req_file), "--count-uncached"],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    )  # fmt: skip
    fresh_requests = int(count.stdout.strip())
    budget = int(config["llm_rpd"])
    used = calls_today(settings)
    if used + fresh_requests > budget:
        raise SystemExit(
            f"{name}: {fresh_requests} uncached requests would exceed today's budget "
            f"({used}/{budget})"
        )
    env = {
        **os.environ,
        "GEMINI_API_KEY": settings.get("GEMINI_API_KEY", ""),
        "BENCH_RPM": config["llm_rpm"],
    }
    print(f"  [{name}] {len(requests)} request(s) ...", flush=True)
    subprocess.run(
        ["node", "bench/llm_runner.mjs", str(req_file), str(out_file)],
        env=env,
        check=True,
        cwd=REPO_ROOT,
    )
    results = {
        r["id"]: r for r in map(json.loads, out_file.read_text(encoding="utf-8").splitlines())
    }
    fresh = [r for r in results.values() if not r["cached"]]
    if fresh:
        with (
            psycopg.connect(**admin_connect_kwargs(settings, "local")) as conn,
            conn.cursor() as cur,
        ):
            for r in fresh:
                for i, a in enumerate(r["attempts"], start=1):
                    last = i == len(r["attempts"])
                    cur.execute(
                        "INSERT INTO llm_calls (execution_id, purpose, provider, model, attempt, "
                        "http_status, outcome, tokens_in, tokens_out, duration_ms, error) "
                        "VALUES (%s, %s, 'gemini', %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            f"bench:{name}",
                            f"bench:{name}",
                            r["model"],
                            i,
                            a["status"] or None,
                            a["outcome"],
                            r["tokens_in"] if last else 0,
                            r["tokens_out"] if last else 0,
                            a["ms"],
                            None if (r["ok"] and last) else r.get("error"),
                        ),
                    )
    return results


# ---------------------------------------------------------------------------------------------
# Data: the batch, its fuzzy (pair, criterion) rows, and the oracle sample
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FuzzyRow:
    """One (user, product, fuzzy criterion) that passed the hard filter."""

    user_id: str
    product_id: int
    criterion_key: str
    criterion_text: str
    depends_on: tuple[str, ...]


def ensure_batch(n: int = 10_000, seed: int = 42) -> tuple[psycopg.Connection, str]:
    """Reuse (or build) the synthetic bench DB with one 10k batch for the LLM experiments."""
    try:
        conn = benchdb.connect()
        row = conn.execute(
            "SELECT batch_id::text FROM ingestion_batches WHERE s3_key = %s",
            (f"bench/{BATCH_LABEL}.csv",),
        ).fetchone()
        synthetic = conn.execute(
            "SELECT count(*) FROM crawl_sources WHERE slug = 'synthetic-bench'"
        ).fetchone()
        if row and synthetic and synthetic[0]:
            return conn, str(row[0])
        conn.close()
    except psycopg.OperationalError:
        pass
    benchdb.recreate("synthetic")
    conn = benchdb.connect()
    return conn, benchdb.load_batch(conn, generate(n, seed, "real"), BATCH_LABEL)


def fuzzy_rows(conn: psycopg.Connection, batch: str) -> list[FuzzyRow]:
    """Every pending (pair, criterion) of the batch."""
    rows = conn.execute(
        "SELECT user_id::text, product_id, criterion_key, criterion_text, depends_on "
        "FROM fuzzy_signatures(%s, 'exact')",
        (batch,),
    ).fetchall()
    return [FuzzyRow(u, p, k, t, tuple(d)) for u, p, k, t, d in rows]


def signatures(conn: psycopg.Connection, batch: str, mode: str) -> dict[tuple[str, int, str], str]:
    """(user, product, criterion_key) -> banded signature under one banding mode."""
    rows = conn.execute(
        "SELECT user_id::text, product_id, criterion_key, signature FROM fuzzy_signatures(%s, %s)",
        (batch, mode),
    ).fetchall()
    return {(u, p, k): s for u, p, k, s in rows}


def user_facts(conn: psycopg.Connection, batch: str) -> dict[str, dict[str, Any]]:
    """Exact facts per user."""
    rows = conn.execute(
        "SELECT user_id::text, monthly_income::int, credit_score, employment_status, age "
        "FROM users WHERE batch_id = %s",
        (batch,),
    ).fetchall()
    return {
        u: {"monthly_income": inc, "credit_score": cs, "employment_status": emp, "age": age}
        for u, inc, cs, emp, age in rows
    }


def oracle_sample(
    rows: list[FuzzyRow], seed: int = 7, users: int = 12, per_instance: int = 6
) -> list[FuzzyRow]:
    """All fuzzy rows of a few users, topped up per (product, criterion) instance.

    Taking every row of the chosen users lets per-user calls be compared on the same pairs; the
    top-up gives every instance at least ``per_instance`` pairs.
    """
    rng = random.Random(seed)
    by_user: dict[str, list[FuzzyRow]] = defaultdict(list)
    for r in rows:
        by_user[r.user_id].append(r)
    rich = sorted(u for u, rs in by_user.items() if len(rs) >= 4)
    chosen = set(rng.sample(rich, users))
    sample = [r for r in rows if r.user_id in chosen]
    counts = Counter((r.product_id, r.criterion_key) for r in sample)
    by_instance: dict[tuple[int, str], list[FuzzyRow]] = defaultdict(list)
    for r in rows:
        if r.user_id not in chosen:
            by_instance[(r.product_id, r.criterion_key)].append(r)
    for inst, pool in sorted(by_instance.items()):
        need = per_instance - counts[inst]
        if need > 0:
            sample.extend(rng.sample(pool, min(need, len(pool))))
    return sorted(sample, key=lambda r: (r.criterion_key, r.user_id, r.product_id))


def exact_signature(facts: dict[str, Any], fields: Iterable[str] = FACTS) -> str:
    """A signature carrying exact values (the oracle's view of a user)."""
    return ";".join(f"{f}={facts[f]}" for f in sorted(fields))


# ---------------------------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------------------------
@dataclass
class Outcome:
    """Verdicts on the oracle sample, plus what the strategy cost on this batch."""

    name: str
    verdicts: dict[tuple[str, int, str], str]
    calls: int
    tokens_in: int
    tokens_out: int
    ms: int
    questions: int
    notes: dict[str, Any] = field(default_factory=dict)


def cohort_strategy(
    name: str,
    config: dict[str, str],
    rows: list[FuzzyRow],
    sigs: dict[tuple[str, int, str], str],
    sample: list[FuzzyRow],
    k: int,
    *,
    only_sample_cohorts: bool = False,
    batch_mode: str = "packed",
) -> Outcome:
    """L4: ask once per distinct (criterion, signature), K cohorts per request.

    batch_mode 'stratified' puts at most one cohort of each criterion in a request.
    """
    wanted = {(r.criterion_key, sigs[(r.user_id, r.product_id, r.criterion_key)]) for r in sample}
    cohorts: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        sig = sigs[(r.user_id, r.product_id, r.criterion_key)]
        if only_sample_cohorts and (r.criterion_key, sig) not in wanted:
            continue
        cohorts.setdefault(
            (r.criterion_key, sig),
            {
                "criterion_key": r.criterion_key,
                "criterion_text": r.criterion_text,
                "depends_on": list(r.depends_on),
                "signature": sig,
            },
        )
    requests = code_node(
        "build_cohort_requests.js",
        [
            {
                "config": {**config, "llm_batch_k": str(k), "llm_batch_mode": batch_mode},
                "cohorts": list(cohorts.values()),
            }
        ],
    )
    for i, req in enumerate(requests):
        req["id"] = f"{name}-{i}"
    results = run_llm(name, requests, config)
    items = [{**req, **results[req["id"]]} for req in requests]
    collected = code_node("parse_cohort_verdicts.js", items, {})[0]
    table = {(d["criterion_key"], d["signature"]): d["verdict"] for d in collected["decisions"]}
    verdicts = {
        (r.user_id, r.product_id, r.criterion_key): table.get(
            (r.criterion_key, sigs[(r.user_id, r.product_id, r.criterion_key)]), "missing"
        )
        for r in sample
    }
    return Outcome(
        name,
        verdicts,
        len(requests),
        collected["tokens_in"],
        collected["tokens_out"],
        sum(r["ms"] for r in results.values()),
        len(cohorts),
        {
            "unanswered": collected["unanswered"],
            "failed_requests": collected["failed_requests"],
            "k": k,
            "batch_mode": batch_mode,
            "uncertain_cohorts": sum(1 for v in table.values() if v == "uncertain"),
        },
    )


def oracle(
    config: dict[str, str],
    sample: list[FuzzyRow],
    facts: dict[str, dict[str, Any]],
    *,
    name: str = "oracle",
    salt: str | None = None,
) -> Outcome:
    """One question per sampled pair, all four facts exact, K = 1 (a cohort of one user).

    With a salt, the same questions are asked again and cached separately: the oracle's own
    self-consistency, i.e. the noise floor no cheaper strategy can be expected to beat.
    """
    requests = []
    for i, r in enumerate(sample):
        cohort = {
            "criterion_key": r.criterion_key,
            "criterion_text": r.criterion_text,
            "depends_on": list(FACTS),
            "signature": exact_signature(facts[r.user_id]),
        }
        req = code_node(
            "build_cohort_requests.js",
            [{"config": {**config, "llm_batch_k": "1"}, "cohorts": [cohort]}],
        )[0]
        req["id"] = f"{name}-{i}"
        if salt:
            req["cache_salt"] = salt
        requests.append(req)
    results = run_llm(name, requests, config)
    verdicts = {}
    for req, r in zip(requests, sample, strict=True):
        res = results[req["id"]]
        v = (res.get("data") or {}).get("verdicts") or [{}]
        verdicts[(r.user_id, r.product_id, r.criterion_key)] = (
            v[0].get("verdict", "missing") if res["ok"] else "missing"
        )
    return Outcome(
        name,
        verdicts,
        len(requests),
        sum(x["tokens_in"] for x in results.values()),
        sum(x["tokens_out"] for x in results.values()),
        sum(x["ms"] for x in results.values()),
        len(requests),
    )


def judge_system_prompt() -> str:
    """The production judging prompt, read from the Code-node file so the two can't drift."""
    source = (REPO_ROOT / "n8n" / "code" / "build_cohort_requests.js").read_text(encoding="utf-8")
    match = re.search(r"const SYSTEM = `(.*?)`;", source, re.S)
    assert match
    return match.group(1)


def per_user(
    config: dict[str, str], sample: list[FuzzyRow], facts: dict[str, dict[str, Any]]
) -> Outcome:
    """L2: one call per user with all of that user's sampled criteria (exact facts)."""
    by_user: dict[str, list[FuzzyRow]] = defaultdict(list)
    for r in sample:
        by_user[r.user_id].append(r)
    users = [u for u, rs in by_user.items() if len(rs) >= 3]
    requests: list[dict[str, Any]] = []
    index: dict[tuple[str, int], FuzzyRow] = {}
    for i, u in enumerate(users):
        f = facts[u]
        lines = [
            "Judge every question for this applicant. Answer each question id exactly once.",
            "",
            f"Applicant: age {f['age']} years; credit score {f['credit_score']}; employment: "
            f"{f['employment_status']}; monthly income ₹{f['monthly_income']:,}",
            "",
        ]
        for j, r in enumerate(by_user[u], start=1):
            lines.append(f'  {j}. Criterion: "{r.criterion_text}"')
            index[(f"per_user-{i}", j)] = r
        n = len(by_user[u])
        requests.append(
            {
                "id": f"per_user-{i}",
                "purpose": "bench_per_user",
                "model": config["llm_model_match"],
                "temperature": 0,
                "max_output_tokens": 1024 + 96 * n,
                "system": judge_system_prompt(),
                "prompt": "\n".join(lines),
                "schema": {
                    "type": "object",
                    "required": ["verdicts"],
                    "additionalProperties": False,
                    "properties": {
                        "verdicts": {
                            "type": "array",
                            "minItems": n,
                            "maxItems": n,
                            "items": {
                                "type": "object",
                                "required": ["id", "verdict", "reason"],
                                "additionalProperties": False,
                                "properties": {
                                    "id": {"type": "integer"},
                                    "reason": {"type": "string"},
                                    "verdict": {
                                        "type": "string",
                                        "enum": ["eligible", "ineligible", "uncertain"],
                                    },
                                },
                            },
                        }
                    },
                },
            }
        )
    results = run_llm("per_user", requests, config)
    verdicts = {}
    for req in requests:
        res = results[req["id"]]
        for v in (res.get("data") or {}).get("verdicts", []) if res["ok"] else []:
            hit = index.get((req["id"], v.get("id")))
            if hit:
                verdicts[(hit.user_id, hit.product_id, hit.criterion_key)] = v["verdict"]
    return Outcome(
        "per_user",
        verdicts,
        len(requests),
        sum(x["tokens_in"] for x in results.values()),
        sum(x["tokens_out"] for x in results.values()),
        sum(x["ms"] for x in results.values()),
        len(index),
        {"users": len(users)},
    )


COMPILE_SYSTEM = (
    "You turn a lender's qualitative eligibility criterion into explicit conditions on the "
    "applicant facts it depends on, so a database can apply it without asking again. Use "
    "your judgment the way a careful loan officer would. Facts: age (years), credit_score "
    "(300-900 CIBIL), employment_status (exactly one of Salaried, Self-Employed, "
    "Business), monthly_income (INR per month).\n"
    "Express each criterion as clauses: an applicant meets the criterion when any one "
    "clause matches. A clause matches when every condition in it holds. Leave a bound null "
    "when it does not restrict, and leave employment_status empty when any employment "
    "qualifies. If no explicit conditions can capture the criterion, set undecidable to "
    "true."
)


def compile_strategy(
    config: dict[str, str],
    rows: list[FuzzyRow],
    sample: list[FuzzyRow],
    facts: dict[str, dict[str, Any]],
) -> Outcome:
    """L5: one call compiles every distinct fuzzy criterion into DNF conditions; SQL decides."""
    criteria = sorted({(r.criterion_key, r.criterion_text, r.depends_on) for r in rows})
    lines = ["Compile each criterion. Answer each id exactly once.", ""]
    for i, (_, text, deps) in enumerate(criteria, start=1):
        lines.append(f'{i}. "{text}" (depends on: {", ".join(deps)})')
    bound = {"type": ["number", "null"]}
    clause = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "employment_status": {
                "type": "array",
                "items": {"type": "string", "enum": ["Salaried", "Self-Employed", "Business"]},
            },
            **{
                f"{f}_{e}": bound
                for f in ("age", "credit_score", "monthly_income")
                for e in ("min", "max")
            },
        },
        "required": [
            "employment_status",
            "age_min",
            "age_max",
            "credit_score_min",
            "credit_score_max",
            "monthly_income_min",
            "monthly_income_max",
        ],
    }
    n = len(criteria)
    request = {
        "id": "compile-0",
        "purpose": "bench_compile",
        "model": config["llm_model_match"],
        "temperature": 0,
        "max_output_tokens": 2048 + 256 * n,
        "system": COMPILE_SYSTEM,
        "prompt": "\n".join(lines),
        "schema": {
            "type": "object",
            "required": ["rules"],
            "additionalProperties": False,
            "properties": {
                "rules": {
                    "type": "array",
                    "minItems": n,
                    "maxItems": n,
                    "items": {
                        "type": "object",
                        "required": ["id", "clauses", "undecidable", "note"],
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "integer"},
                            "clauses": {"type": "array", "items": clause},
                            "undecidable": {"type": "boolean"},
                            "note": {"type": "string"},
                        },
                    },
                }
            },
        },
    }
    res = run_llm("compile", [request], config)["compile-0"]
    rules = {}
    for rule in (res.get("data") or {}).get("rules", []) if res["ok"] else []:
        if 1 <= rule.get("id", 0) <= n:
            rules[criteria[rule["id"] - 1][0]] = rule

    def holds(rule: dict[str, Any] | None, f: dict[str, Any]) -> str:
        if not rule:
            return "missing"
        if rule["undecidable"]:
            return "uncertain"
        for c in rule["clauses"]:
            if c["employment_status"] and f["employment_status"] not in c["employment_status"]:
                continue
            if all(
                (c[f"{x}_min"] is None or f[x] >= c[f"{x}_min"])
                and (c[f"{x}_max"] is None or f[x] <= c[f"{x}_max"])
                for x in ("age", "credit_score", "monthly_income")
            ):
                return "eligible"
        return "ineligible"

    verdicts = {
        (r.user_id, r.product_id, r.criterion_key): holds(
            rules.get(r.criterion_key), facts[r.user_id]
        )
        for r in sample
    }
    return Outcome(
        "compile",
        verdicts,
        1,
        res["tokens_in"],
        res["tokens_out"],
        res["ms"],
        n,
        {"rules": {criteria[i][1]: rules.get(criteria[i][0]) for i in range(n)}},
    )


def compile3_strategy(
    config: dict[str, str],
    rows: list[FuzzyRow],
    sample: list[FuzzyRow],
    facts: dict[str, dict[str, Any]],
) -> Outcome:
    """L5b, the rule judge: compile with an abstain region, one call for all criteria.

    Each criterion becomes clauses tagged eligible or ineligible; profiles no clause covers (or
    covered both ways) are uncertain, which keeps the match. No match-time calls.

    The request comes from the production Code node (build_cohort_requests.js, compile request),
    so the prompt validated here is the prompt Workflow B sends. (A first version with separate
    eligible / ineligible clause lists was rejected by Gemini with HTTP 400: one tagged list is
    the same logic in a schema it accepts.)
    """
    criteria = sorted({(r.criterion_key, r.criterion_text, r.depends_on) for r in rows})
    items = [
        {
            "cohorts": [],
            "criteria": [
                {"criterion_key": k, "criterion_text": text, "depends_on": list(deps)}
                for k, text, deps in criteria
            ],
        }
    ]
    (request,) = code_node("build_cohort_requests.js", [{**items[0], "config": config}])
    request["id"] = "compile3-0"
    n = len(criteria)
    res = run_llm("compile3", [request], config)["compile3-0"]
    keys = {ref["id"]: ref["criterion_key"] for ref in request["criteria_refs"]}
    rules = {}
    for rule in (res.get("data") or {}).get("rules", []) if res["ok"] else []:
        if rule.get("id") in keys:
            rules[keys[rule["id"]]] = rule

    def matches(c: dict[str, Any], f: dict[str, Any]) -> bool:
        if c["employment_status"] and f["employment_status"] not in c["employment_status"]:
            return False
        return all(
            (c[f"{x}_min"] is None or f[x] >= c[f"{x}_min"])
            and (c[f"{x}_max"] is None or f[x] <= c[f"{x}_max"])
            for x in ("age", "credit_score", "monthly_income")
        )

    def decide(rule: dict[str, Any] | None, f: dict[str, Any]) -> str:
        if not rule:
            return "missing"
        yes = any(matches(c, f) for c in rule["clauses"] if c["verdict"] == "eligible")
        no = any(matches(c, f) for c in rule["clauses"] if c["verdict"] == "ineligible")
        return "eligible" if yes and not no else "ineligible" if no and not yes else "uncertain"

    verdicts = {
        (r.user_id, r.product_id, r.criterion_key): decide(
            rules.get(r.criterion_key), facts[r.user_id]
        )
        for r in sample
    }
    return Outcome(
        "compile3",
        verdicts,
        1,
        res["tokens_in"],
        res["tokens_out"],
        res["ms"],
        n,
        {"rules": {criteria[i][1]: rules.get(criteria[i][0]) for i in range(n)}},
    )


# ---------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------
def keep(verdict: str) -> bool | None:
    """The decision a verdict leads to: keep the match ('may qualify') unless clearly ineligible."""
    return None if verdict == "missing" else verdict != "ineligible"


def agreement(outcome: Outcome, reference: Outcome) -> dict[str, Any]:
    """Agreement with the oracle on the pairs both answered.

    The two errors are not equally bad. A false drop (the oracle keeps the pair, the strategy
    rejects it) hides a product the user may qualify for; a false keep only adds a "may
    qualify" line. So besides overall agreement this reports:
        keep_recall   share of the oracle's keeps the strategy also keeps (1 - false-drop rate)
        drop_recall   share of the oracle's rejections the strategy also rejects: how much of
                      the LLM stage's filtering the strategy delivers (rules_only has 0)
    """
    shared = [
        k
        for k in outcome.verdicts
        if k in reference.verdicts
        and keep(outcome.verdicts[k]) is not None
        and keep(reference.verdicts[k]) is not None
    ]
    if not shared:
        return {"compared": 0}
    binary = sum(keep(outcome.verdicts[k]) == keep(reference.verdicts[k]) for k in shared) / len(
        shared
    )
    exact = sum(outcome.verdicts[k] == reference.verdicts[k] for k in shared) / len(shared)
    oracle_keeps = [k for k in shared if keep(reference.verdicts[k])]
    oracle_drops = [k for k in shared if not keep(reference.verdicts[k])]
    false_drops = sum(1 for k in oracle_keeps if not keep(outcome.verdicts[k]))
    false_keeps = sum(1 for k in oracle_drops if keep(outcome.verdicts[k]))
    return {
        "compared": len(shared),
        "decision_agreement": round(binary, 3),
        "verdict_agreement": round(exact, 3),
        "false_drops": false_drops,
        "false_keeps": false_keeps,
        "keep_recall": round(1 - false_drops / len(oracle_keeps), 3) if oracle_keeps else None,
        "drop_recall": round(1 - false_keeps / len(oracle_drops), 3) if oracle_drops else None,
    }


def with_rule_veto(cohort: str, rule: str) -> str:
    """The production combination (write_matches.sql, voted CTE) of a cohort verdict and a rule.

    The cohort verdict decides; the rule can only veto an uncertain one ('ineligible'). It never
    vouches: its 'eligible' leaves 'uncertain' as it is, so the caveat stays. A disagreement
    gives 'uncertain', so neither judge can drop a pair the other affirms. ('missing' from the
    rule abstains; 'missing' from the cohort judge stays missing.)
    """
    if rule not in ("eligible", "ineligible") or rule == cohort or cohort == "missing":
        return cohort
    return "ineligible" if cohort == "uncertain" and rule == "ineligible" else "uncertain"


def instance_spread(pool: list[FuzzyRow], n: int, rng: random.Random) -> list[FuzzyRow]:
    """Up to n rows, taken round-robin across (product, criterion) instances."""
    groups: dict[tuple[int, str], list[FuzzyRow]] = defaultdict(list)
    for r in pool:
        groups[(r.product_id, r.criterion_key)].append(r)
    queues = [rng.sample(g, len(g)) for _, g in sorted(groups.items())]
    picked: list[FuzzyRow] = []
    while len(picked) < n and any(queues):
        for q in queues:
            if q and len(picked) < n:
                picked.append(q.pop())
    return picked


def holdout(
    config: dict[str, str],
    rows: list[FuzzyRow],
    facts: dict[str, dict[str, Any]],
    sigs: dict[str, dict[tuple[str, int, str], str]],
    seen: list[FuzzyRow],
    *,
    drops: int,
    keeps: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Validate the chosen strategy on pairs the first sample never saw.

    The sample is stratified by the strategy's own decision: pairs it drops (does the oracle
    agree? every disagreement is a false drop) and pairs it keeps (how many rejections does it
    miss?). Both judges' answers come from the cache; only the oracle questions are new.
    """
    every = cohort_strategy("cohort_threshold_k25", config, rows, sigs["threshold"], rows, 25)
    rules = compile3_strategy(config, rows, rows, facts)

    def key(r: FuzzyRow) -> tuple[str, int, str]:
        return (r.user_id, r.product_id, r.criterion_key)

    taken = {key(r) for r in seen}
    pool = [r for r in rows if key(r) not in taken]
    decided = {key(r): with_rule_veto(every.verdicts[key(r)], rules.verdicts[key(r)]) for r in pool}
    drop_pool = [r for r in pool if decided[key(r)] == "ineligible"]
    keep_pool = [r for r in pool if decided[key(r)] != "ineligible"]
    rng = random.Random(8)
    strata = {
        "dropped": instance_spread(drop_pool, drops, rng),
        "kept": instance_spread(keep_pool, keeps, rng),
    }
    print(
        f"holdout: pool {len(pool):,} rows, the chosen strategy drops {len(drop_pool):,} "
        f"({len(drop_pool) / len(pool):.1%}); sampling {len(strata['dropped'])} dropped + "
        f"{len(strata['kept'])} kept"
    )
    if dry_run:
        return {}
    sample = strata["dropped"] + strata["kept"]
    ref = oracle(config, sample, facts, name="holdout_oracle")
    judges = {
        "threshold_k25": every.verdicts,
        "compile3": rules.verdicts,
        "with_rule_veto": decided,
    }
    report: dict[str, Any] = {
        "pool_rows": len(pool),
        "strategy_drops": len(drop_pool),
        "oracle_verdicts": dict(Counter(ref.verdicts.values())),
        "strata": {},
    }
    for stratum, rows_ in strata.items():
        entry: dict[str, Any] = {"pairs": len(rows_)}
        for name, verdicts in judges.items():
            pairs = [
                (verdicts[key(r)], ref.verdicts[key(r)])
                for r in rows_
                if ref.verdicts[key(r)] != "missing"
            ]
            entry[name] = {
                "false_drops": sum(1 for v, o in pairs if not keep(v) and keep(o)),
                "missed_drops": sum(1 for v, o in pairs if keep(v) and not keep(o)),
                "agree": sum(1 for v, o in pairs if keep(v) == keep(o)),
                "compared": len(pairs),
            }
        entry["oracle_rejects"] = sum(1 for r in rows_ if ref.verdicts[key(r)] == "ineligible")
        by_criterion: dict[str, dict[str, int]] = defaultdict(dict)
        for (text, verdict), n in Counter(
            (r.criterion_text[:48], ref.verdicts[key(r)]) for r in rows_
        ).most_common():
            by_criterion[text][verdict] = n
        entry["oracle_by_criterion"] = by_criterion
        report["strata"][stratum] = entry
    dropped, kept = report["strata"]["dropped"], report["strata"]["kept"]
    # Population estimates: precision of the vote's drops, and the rejections it leaves in
    # its keeps, weighted by the size of each stratum.
    precision = 1 - dropped["with_rule_veto"]["false_drops"] / max(
        1, dropped["with_rule_veto"]["compared"]
    )
    missed_rate = kept["with_rule_veto"]["missed_drops"] / max(
        1, kept["with_rule_veto"]["compared"]
    )
    true_drops = len(drop_pool) * precision
    report["estimates"] = {
        "drop_precision": round(precision, 3),
        "missed_rejection_rate_in_keeps": round(missed_rate, 3),
        "drop_recall": round(true_drops / (true_drops + len(keep_pool) * missed_rate), 3)
        if true_drops
        else None,
    }
    (RESULTS / "holdout.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))
    return report


def main(argv: list[str] | None = None) -> int:
    """Run every strategy on the same oracle sample and write bench/results/llm/summary.json."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--repeat", type=int, default=15, help="oracle questions re-asked for self-consistency"
    )
    parser.add_argument(
        "--holdout",
        action="store_true",
        help="validate the chosen strategy on a fresh sample (bench/results/llm/holdout.json)",
    )
    parser.add_argument("--holdout-drops", type=int, default=40)
    parser.add_argument("--holdout-keeps", type=int, default=60)
    args = parser.parse_args(argv)

    conn, batch = ensure_batch()
    config = {k: v for k, v in conn.execute("SELECT key, value FROM app_config").fetchall()}
    rows = fuzzy_rows(conn, batch)
    facts = user_facts(conn, batch)
    sample = oracle_sample(rows)
    sigs = {m: signatures(conn, batch, m) for m in ("threshold", "coarse", "fine", "exact")}
    cohort_counts = {m: len({(k[2], s) for k, s in sigs[m].items()}) for m in sigs}
    print(
        f"batch: {len(facts):,} users, {len(rows):,} pending (pair, criterion) rows, oracle "
        f"sample {len(sample)} pairs over {len({r.user_id for r in sample})} users; "
        f"cohorts {cohort_counts}"
    )
    if args.holdout:
        holdout(
            config,
            rows,
            facts,
            sigs,
            sample,
            drops=args.holdout_drops,
            keeps=args.holdout_keeps,
            dry_run=args.dry_run,
        )
        return 0
    if args.dry_run:
        return 0

    started = time.time()
    ref = oracle(config, sample, facts)
    outcomes = [
        cohort_strategy("cohort_threshold_k25", config, rows, sigs["threshold"], sample, 25),
        cohort_strategy("cohort_coarse_k25", config, rows, sigs["coarse"], sample, 25),
        cohort_strategy("cohort_fine_k25", config, rows, sigs["fine"], sample, 25),
        cohort_strategy(
            "cohort_exact_k25", config, rows, sigs["exact"], sample, 25, only_sample_cohorts=True
        ),
        cohort_strategy("cohort_coarse_k1", config, rows, sigs["coarse"], sample, 1),
        cohort_strategy("cohort_coarse_k10", config, rows, sigs["coarse"], sample, 10),
        cohort_strategy("cohort_coarse_k50", config, rows, sigs["coarse"], sample, 50),
        cohort_strategy(
            "cohort_coarse_strat_k25",
            config,
            rows,
            sigs["coarse"],
            sample,
            25,
            batch_mode="stratified",
        ),
        per_user(config, sample, facts),
        compile_strategy(config, rows, sample, facts),
        compile3_strategy(config, rows, sample, facts),
    ]
    by_name = {o.name: o for o in outcomes}
    coarse, fine = by_name["cohort_coarse_k25"], by_name["cohort_fine_k25"]
    adaptive_verdicts = {
        k: (fine.verdicts[k] if v == "uncertain" else v) for k, v in coarse.verdicts.items()
    }
    refined = {
        (r.criterion_key, sigs["fine"][(r.user_id, r.product_id, r.criterion_key)])
        for r in rows
        if coarse.verdicts.get((r.user_id, r.product_id, r.criterion_key)) == "uncertain"
    }
    outcomes.append(
        Outcome(
            "adaptive_coarse_then_fine",
            adaptive_verdicts,
            coarse.calls + math.ceil(len(refined) / 25),
            0,
            0,
            0,
            coarse.questions + len(refined),
            {"refined_cohorts_in_sample": len(refined)},
        )
    )
    outcomes.append(Outcome("rules_only", {k: "eligible" for k in ref.verdicts}, 0, 0, 0, 0, 0))
    threshold, rules = by_name["cohort_threshold_k25"], by_name["compile3"]
    outcomes.append(
        Outcome(
            "threshold_with_rule_veto",
            {
                k: with_rule_veto(
                    threshold.verdicts.get(k, "missing"), rules.verdicts.get(k, "missing")
                )
                for k in ref.verdicts
            },
            threshold.calls + rules.calls,
            threshold.tokens_in + rules.tokens_in,
            threshold.tokens_out + rules.tokens_out,
            threshold.ms + rules.ms,
            threshold.questions + rules.questions,
        )
    )
    repeat = oracle(config, sample[: args.repeat], facts, name="oracle_repeat", salt="repeat-1")

    summary: dict[str, Any] = {
        "batch_users": len(facts),
        "pending_rows": len(rows),
        "oracle_pairs": len(sample),
        "cohort_counts": cohort_counts,
        "elapsed_s": round(time.time() - started),
        "oracle": {
            "calls": ref.calls,
            "tokens_in": ref.tokens_in,
            "tokens_out": ref.tokens_out,
            "verdicts": dict(Counter(ref.verdicts.values())),
        },
        "oracle_self_consistency": agreement(repeat, ref),
        "strategies": [
            {
                "name": o.name,
                "calls": o.calls,
                "questions": o.questions,
                "tokens_in": o.tokens_in,
                "tokens_out": o.tokens_out,
                "llm_ms": o.ms,
                **agreement(o, ref),
                "notes": o.notes,
            }
            for o in outcomes
        ],
        "sample_verdicts": [
            {
                "user": k[0][:8],
                "product_id": k[1],
                "criterion": next(r.criterion_text for r in sample if r.criterion_key == k[2]),
                "oracle": v,
                **{o.name: o.verdicts.get(k) for o in outcomes},
            }
            for k, v in ref.verdicts.items()
        ],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(f"\noracle self-consistency: {summary['oracle_self_consistency']}")
    print(
        f"{'strategy':28} {'calls':>5} {'questions':>9} {'tok_in':>7} {'tok_out':>7} "
        f"{'decision':>8} {'verdict':>7}"
    )
    for s in summary["strategies"]:
        print(
            f"{s['name']:28} {s['calls']:>5} {s['questions']:>9} {s['tokens_in']:>7} "
            f"{s['tokens_out']:>7} {s.get('decision_agreement', '-'):>8} "
            f"{s.get('verdict_agreement', '-'):>7}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
