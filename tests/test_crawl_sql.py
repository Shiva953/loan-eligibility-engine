"""Workflow A's SQL against a real Postgres, executed as the n8n_app role that n8n uses.

Running as n8n_app checks the grants and the logic together: if a statement needs a privilege
the role lacks, the test fails here rather than in a crawl.
"""

import json
import re
from typing import Any

import psycopg
import pytest

from scripts import migrate, seed_sources
from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import REPO_ROOT


def sql_file(name: str) -> str:
    """Load a workflow SQL file and convert its $n parameters to psycopg's named style."""
    text = (REPO_ROOT / "sql" / name).read_text(encoding="utf-8").replace("%", "%%")
    return re.sub(r"\$(\d+)", r"%(p\1)s", text)


def params(*values: Any) -> dict[str, Any]:
    return {f"p{i}": v for i, v in enumerate(values, start=1)}


@pytest.fixture
def db(scratch_db: str, local_settings: dict[str, str]) -> psycopg.Connection:
    migrate.run("local", scratch_db)
    seed_sources.main(["--database", scratch_db])
    kwargs = admin_connect_kwargs(local_settings, "local", scratch_db)
    kwargs.update(user="n8n_app", password=local_settings["N8N_APP_DB_PASSWORD"])
    return psycopg.connect(**kwargs, autocommit=True)


def run(conn: psycopg.Connection, name: str, *values: Any) -> list[tuple[Any, ...]]:
    return conn.execute(sql_file(name).encode(), params(*values)).fetchall()


def product(name: str, max_age: int, criteria_hash: str) -> dict[str, Any]:
    criteria = {"max_age": max_age, "min_age": 21, "soft_criteria": []}
    return {
        "lender": "HDFC Bank",
        "product_name": name,
        "source_url": "https://www.hdfc.bank.in/personal-loan/eligibility-criteria",
        "interest_rate_min": None,
        "interest_rate_max": None,
        "min_monthly_income": 25000,
        "min_credit_score": None,
        "min_age": 21,
        "max_age": max_age,
        "allowed_employment": ["Salaried"],
        "soft_criteria": [],
        "field_confidence": {"age": 1},
        "criteria_json": criteria,
        "criteria_hash": criteria_hash,
    }


def source_id(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT source_id FROM crawl_sources WHERE slug = 'hdfc-bank'").fetchone()
    assert row is not None
    return int(row[0])


def test_upsert_records_first_sighting_then_a_diff(db: psycopg.Connection) -> None:
    sid = source_id(db)
    (run_id, _, mode) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    assert mode == "fixtures"

    first = [product("HDFC Personal Loan", 60, "h1"), product("HDFC Top-up", 60, "h2")]
    first_run = run(db, "crawl/upsert_products.sql", json.dumps(first), sid, run_id, "c1")
    assert first_run == [(2, 2, 0, 0)]

    changed = [product("HDFC Personal Loan", 58, "h1b"), product("HDFC Top-up", 60, "h2")]
    assert run(db, "crawl/upsert_products.sql", json.dumps(changed), sid, run_id, "c2") == [
        (2, 0, 1, 0)
    ]

    review: dict[str, bool] = dict(
        db.execute("SELECT product_name, needs_review FROM loan_products").fetchall()
    )
    assert review == {"HDFC Personal Loan": True, "HDFC Top-up": False}
    history = db.execute(
        "SELECT p.product_name, h.old_criteria IS NULL, h.changed_fields "
        "FROM product_criteria_history h JOIN loan_products p USING (product_id) "
        "ORDER BY h.history_id"
    ).fetchall()
    assert history == [
        ("HDFC Personal Loan", True, ["max_age", "min_age", "soft_criteria"]),
        ("HDFC Top-up", True, ["max_age", "min_age", "soft_criteria"]),
        ("HDFC Personal Loan", False, ["max_age"]),
    ]


def test_a_renamed_re_extraction_updates_the_product_it_already_has(
    db: psycopg.Connection,
) -> None:
    # O1: the LLM named the same HDFC product two ways on two readings of an unchanged page.
    sid = source_id(db)
    (run_id, _, _) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    run(db, "crawl/upsert_products.sql", json.dumps([product("HDFC Personal Loan", 60, "h1")]),
        sid, run_id, "c1")  # fmt: skip
    again = [product("HDFC Bank Personal Loan", 60, "h1")]
    again_run = run(db, "crawl/upsert_products.sql", json.dumps(again), sid, run_id, "c1")
    assert again_run == [(1, 0, 0, 0)]
    rows = db.execute("SELECT product_name, identity_key FROM loan_products").fetchall()
    assert rows == [("HDFC Bank Personal Loan", "hdfc|personal loan")]


def test_one_product_listed_twice_in_one_extraction_is_stored_once(db: psycopg.Connection) -> None:
    sid = source_id(db)
    (run_id, _, _) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    sparse = {**product("HDFC Personal Loan", 60, "h1"), "min_monthly_income": None}
    full = product("HDFC Bank Personal Loan", 60, "h1")
    both = json.dumps([sparse, full])
    assert run(db, "crawl/upsert_products.sql", both, sid, run_id, "c1") == [(1, 1, 0, 0)]
    kept = db.execute("SELECT product_name, min_monthly_income FROM loan_products").fetchone()
    assert kept == ("HDFC Bank Personal Loan", 25000)  # the entry with more hard-filter fields


def test_a_product_the_new_extraction_drops_is_retired_at_once(db: psycopg.Connection) -> None:
    sid = source_id(db)
    (run_id, _, _) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    both = [product("HDFC Personal Loan", 60, "h1"), product("HDFC Top-up", 60, "h2")]
    run(db, "crawl/upsert_products.sql", json.dumps(both), sid, run_id, "c1")
    only_one = [product("HDFC Personal Loan", 60, "h1")]
    assert run(db, "crawl/upsert_products.sql", json.dumps(only_one), sid, run_id, "c2") == [
        (1, 0, 0, 1)
    ]
    active: dict[str, bool] = dict(
        db.execute("SELECT product_name, is_active FROM loan_products").fetchall()
    )
    assert active == {"HDFC Personal Loan": True, "HDFC Top-up": False}
    # An empty extraction (a failed read) retires nothing.
    assert run(db, "crawl/upsert_products.sql", "[]", sid, run_id, "c3") == [(0, 0, 0, 0)]
    assert db.execute("SELECT count(*) FROM loan_products WHERE is_active").fetchone() == (1,)


def test_empty_product_list_is_a_no_op(db: psycopg.Connection) -> None:
    sid = source_id(db)
    (run_id, _, _) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    assert run(db, "crawl/upsert_products.sql", "[]", sid, run_id, "c1") == [(0, 0, 0, 0)]


def test_finish_run_bookkeeping(db: psycopg.Connection) -> None:
    sid = source_id(db)
    (run_id, _, _) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    run(db, "crawl/upsert_products.sql", json.dumps([product("P", 60, "h")]), sid, run_id, "c1")
    db.execute("UPDATE loan_products SET last_seen_at = now() - interval '3 days'")

    # 'unchanged' stores the hash and marks the source's products as still on the page.
    finished = run(db, "crawl/finish_run.sql", run_id, "unchanged", 200, "c1", None, 0, 0, 0, None)
    assert finished == [(run_id, "unchanged", 1)]
    hash_row = db.execute(
        "SELECT last_content_hash FROM crawl_sources WHERE source_id = %s", (sid,)
    ).fetchone()
    assert hash_row == ("c1",)
    seen = db.execute("SELECT last_seen_at > now() - interval '1 minute' FROM loan_products")
    assert seen.fetchone() == (True,)

    # 'error' records the run but leaves the source's hash alone, so tomorrow retries.
    (run2, _, _) = run(db, "crawl/start_run.sql", "exec-2", sid, "fixtures")[0]
    run(db, "crawl/finish_run.sql", run2, "error", 503, "zzz", None, 5, 0, 0, "busy")
    hash_row = db.execute(
        "SELECT last_content_hash FROM crawl_sources WHERE source_id = %s", (sid,)
    ).fetchone()
    assert hash_row == ("c1",)
    summary = run(db, "crawl/summary.sql", "exec-2")
    assert [(r[0], r[1], r[7]) for r in summary] == [("hdfc-bank", "error", "busy")]


def test_stale_products_are_deactivated(db: psycopg.Connection) -> None:
    sid = source_id(db)
    (run_id, _, _) = run(db, "crawl/start_run.sql", "exec-1", sid, "fixtures")[0]
    run(db, "crawl/upsert_products.sql", json.dumps([product("P", 60, "h")]), sid, run_id, "c1")
    assert run(db, "crawl/deactivate_stale.sql", 7) == [(0,)]
    db.execute("UPDATE loan_products SET last_seen_at = now() - interval '8 days'")
    assert run(db, "crawl/deactivate_stale.sql", 7) == [(1,)]


def test_load_sources_derives_robots_url(db: psycopg.Connection) -> None:
    rows = {r[1]: r[7] for r in run(db, "crawl/load_sources.sql")}
    assert rows == {
        "bankbazaar": "https://www.bankbazaar.com/robots.txt",
        "hdfc-bank": "https://www.hdfc.bank.in/robots.txt",
        "kotak": "https://www.kotak.bank.in/robots.txt",
    }


def test_a_multi_line_error_is_logged_as_text(db: psycopg.Connection) -> None:
    # Gemini's errors are bullet lists. Before V35 the value was also pasted into the "#11 error"
    # comment, the newline ended the comment, and "* Generate..." was parsed as SQL.
    error = "* GenerateContentRequest.contents[0]: required\n* -- not a comment; $1 'quoted'\n"
    (row,) = run(db, "llm/log_call.sql", "e", "extract_products", "gemini", "m", 1, 400, "error",
                 0, 0, 5, error)  # fmt: skip
    stored = db.execute("SELECT error FROM llm_calls WHERE call_id = %s", (row[0],)).fetchone()
    assert stored == (error,)


def test_llm_ledger_budget_ignores_rate_limited_calls(db: psycopg.Connection) -> None:
    for outcome in ("ok", "rate_limited", "error"):
        run(db, "llm/log_call.sql", "e", "extract_products", "gemini", "m", 1, 200, outcome,
            10, 5, None, None)  # fmt: skip
    ((_config, calls_today),) = run(db, "llm/config_and_usage.sql")
    assert calls_today == 2
