"""Workflow C's SQL against a real Postgres, run as n8n_app the way n8n runs it.

The properties that keep email safe are checked here, not only in n8n: who is recorded, how
many emails can be queued, which addresses they go to, that a CSV address never appears, and
that a rerun queues nothing.
"""

import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from bench import benchdb
from scripts import migrate, seed_config, seed_sources
from scripts.dbconn import admin_connect_kwargs
from scripts.gen_users import generate
from scripts.n8n_sql import run_dicts

Conns = tuple[psycopg.Connection, psycopg.Connection]
ALIASES = ["shivaset2+1@gmail.com", "shivaset2+2@gmail.com"]


@pytest.fixture
def env(scratch_db: str, local_settings: dict[str, str]) -> Iterator[Conns]:
    """(admin, n8n_app) connections to a migrated scratch database with the synthetic products."""
    migrate.run("local", scratch_db)
    seed_config.store("local", seed_config.build_config(local_settings), scratch_db)
    seed_sources.main(["--database", scratch_db])
    kwargs = admin_connect_kwargs(local_settings, "local", scratch_db)
    admin = psycopg.connect(**kwargs, autocommit=True)
    benchdb.load_synthetic_products(admin)
    app_kwargs = {**kwargs, "user": "n8n_app", "password": local_settings["N8N_APP_DB_PASSWORD"]}
    app = psycopg.connect(**app_kwargs, autocommit=True)
    try:
        yield admin, app
    finally:
        app.close()
        admin.close()


def matched_batch(
    admin: psycopg.Connection, app: psycopg.Connection, n: int, seed: int
) -> tuple[str, list[dict[str, Any]]]:
    """A batch whose matches are written (rules-only, so no LLM is involved) and status matched."""
    users = generate(n, seed, "real")
    batch_id = benchdb.load_batch(admin, users, f"notify-{seed}")
    run_dicts(app, "match/write_matches.sql", batch_id, "threshold", "false", "test", "false")
    admin.execute(
        "UPDATE ingestion_batches SET status = 'matched', matched_at = now() WHERE batch_id = %s",
        (batch_id,),
    )
    return batch_id, users


def configure(admin: psycopg.Connection, **values: str) -> None:
    """Set app_config keys (the planning query reads its switches from there, like n8n)."""
    for key, value in values.items():
        admin.execute("UPDATE app_config SET value = %s WHERE key = %s", (value, key))


def plan(
    admin: psycopg.Connection, app: psycopg.Connection, batch_id: str, mode: str, cap: int = 2
) -> dict[str, Any]:
    configure(admin, email_mode=mode, email_cap=str(cap), email_aliases=",".join(ALIASES))
    return run_dicts(app, "notify/plan_notifications.sql", batch_id, "t")[0]


def record(app: psycopg.Connection, notification_id: int, outcome: str, *rest: str) -> Any:
    ses_id, error = rest if rest else ("", "")
    return run_dicts(
        app, "notify/record_notification.sql", notification_id, outcome, ses_id, error, "t"
    )[0]


def batch_status(conn: psycopg.Connection, batch_id: str) -> Any:
    row = conn.execute("SELECT status FROM ingestion_batches WHERE batch_id = %s", (batch_id,))
    return row.fetchone()


def notify_metric(conn: psycopg.Connection, batch_id: str) -> Any:
    row = conn.execute(
        "SELECT details FROM pipeline_metrics WHERE batch_id = %s AND stage = 'notify'",
        (batch_id,),
    ).fetchall()
    return [r[0] for r in row]


def notifications(conn: psycopg.Connection, batch_id: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT user_id::text, status, recipient FROM notifications WHERE batch_id = %s "
        "ORDER BY user_id",
        (batch_id,),
    ).fetchall()


def users_with_matches(conn: psycopg.Connection, batch_id: str) -> int:
    row = conn.execute(
        "SELECT count(DISTINCT user_id) FROM matches WHERE batch_id = %s "
        "AND status IN ('matched', 'llm_matched')",
        (batch_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def test_suppressed_mode_records_everyone_and_queues_nothing(env: Conns) -> None:
    admin, app = env
    batch_id, _ = matched_batch(admin, app, 60, 41)
    out = plan(admin, app, batch_id, "suppressed")
    expected = users_with_matches(admin, batch_id)
    assert (out["users_with_matches"], out["to_send"], out["suppressed"]) == (expected, 0, expected)
    rows = notifications(admin, batch_id)
    assert len(rows) == expected
    assert {status for _, status, _ in rows} == {"suppressed"}
    assert {recipient for _, _, recipient in rows} == {None}
    # The first EMAIL_CAP users still get a rendered preview, which is never sent.
    assert len(out["emails"]) == 2
    assert all(e["status"] == "suppressed" and e["recipient"] is None for e in out["emails"])
    # Nothing to send, so the same statement closes the batch and writes the notify metric.
    assert out["batch_status"] == "notified"
    assert batch_status(admin, batch_id) == ("notified",)
    assert notify_metric(admin, batch_id) == [
        {"sent": 0, "suppressed": expected, "failed": 0, "pending": 0}
    ]


def test_test_alias_mode_queues_exactly_the_cap_to_the_aliases_in_order(env: Conns) -> None:
    admin, app = env
    batch_id, users = matched_batch(admin, app, 60, 42)
    out = plan(admin, app, batch_id, "test_aliases")
    assert (out["to_send"], out["batch_status"]) == (2, "notifying")
    rows = notifications(admin, batch_id)
    pending = [(u, r) for u, status, r in rows if status == "pending"]
    # The two smallest user ids (rows are ordered by user id) get the aliases in order.
    assert pending == [(rows[0][0], ALIASES[0]), (rows[1][0], ALIASES[1])]
    assert all(status == "suppressed" and r is None for _, status, r in rows[2:])
    assert [e["recipient"] for e in out["emails"]] == ALIASES
    # No address from the upload appears anywhere in what the workflow receives.
    payload = json.dumps(out)
    assert not any(u["email"] in payload for u in users)
    assert all("email" not in e for e in out["emails"])


def test_a_rerun_queues_nothing(env: Conns) -> None:
    admin, app = env
    batch_id, _ = matched_batch(admin, app, 40, 43)
    plan(admin, app, batch_id, "test_aliases")
    again = plan(admin, app, batch_id, "test_aliases")
    assert (again["users_with_matches"], again["to_send"], again["emails"]) == (0, 0, [])
    assert sum(1 for _, status, _ in notifications(admin, batch_id) if status == "pending") == 2


def test_recording_results_marks_only_the_sent_users_matches(env: Conns) -> None:
    admin, app = env
    batch_id, _ = matched_batch(admin, app, 40, 44)
    first, second = plan(admin, app, batch_id, "test_aliases")["emails"]
    sent = record(app, first["notification_id"], "sent", "ses-1", "")
    assert sent["status"] == "sent"
    assert sent["matches_marked"] > 0
    assert sent["batch_finished"] is False  # the second email is still pending
    # Recording again changes nothing: only a pending row can change.
    repeat = record(app, first["notification_id"], "failed", "", "x")
    assert (repeat["status"], repeat["batch_finished"]) == (None, False)
    failed = record(app, second["notification_id"], "failed", "", "throttled")
    assert (failed["status"], failed["matches_marked"]) == ("failed", 0)
    # The last pending email closes the batch.
    assert (failed["batch_finished"], failed["metrics_written"]) == (True, 1)
    marked = admin.execute(
        "SELECT count(DISTINCT user_id) FROM matches "
        "WHERE batch_id = %s AND notified_at IS NOT NULL",
        (batch_id,),
    ).fetchone()
    assert marked == (1,)
    assert batch_status(admin, batch_id) == ("notified",)
    (details,) = notify_metric(admin, batch_id)
    assert (details["sent"], details["failed"], details["pending"]) == (1, 1, 0)


def test_a_product_listed_twice_appears_once_from_the_lenders_own_page(env: Conns) -> None:
    admin, app = env
    sources: dict[str, int] = dict(
        admin.execute("SELECT slug, source_id FROM crawl_sources").fetchall()
    )
    criteria: dict[str, Any] = {"min_monthly_income": None, "soft_criteria": []}
    benchdb.insert_products(
        admin,
        [
            (
                sources["bankbazaar"],
                "HDFC Bank",
                "HDFC Bank Personal Loan",
                "https://bb.invalid/pl",
                10.85,
                21.0,
                None,
                None,
                18,  # one published criterion, so the product can match (migration 014)
                None,
                None,
                [],
                {},
                criteria,
                "a",
                "a",
            ),
            (
                sources["hdfc-bank"],
                "HDFC Bank",
                "HDFC Personal Loan",
                "https://hdfc.invalid/pl",
                None,
                None,
                None,
                None,
                18,
                None,
                None,
                [],
                {},
                criteria,
                "b",
                "b",
            ),
        ],
    )
    batch_id, _ = matched_batch(admin, app, 10, 45)
    emails = plan(admin, app, batch_id, "suppressed")["emails"]
    assert emails
    for email in emails:
        hdfc = [p for p in email["products"] if p["lender"] == "HDFC Bank"]
        assert [p["product"] for p in hdfc] == ["HDFC Personal Loan"]


def test_with_no_matched_batch_the_plan_is_empty(env: Conns) -> None:
    admin, app = env
    out = plan(admin, app, "", "test_aliases")
    assert (out["batch_id"], out["users_with_matches"], out["emails"]) == (None, 0, [])
    assert (out["batch_status"], out["metrics_written"]) == (None, 0)


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        ({"email_mode": "live"}, "EMAIL_MODE"),
        ({"email_mode": ""}, "EMAIL_MODE"),
        ({"email_cap": "3"}, "EMAIL_CAP must be between 1 and 2"),
        ({"email_cap": "0"}, "EMAIL_CAP"),
        ({"email_cap": "two"}, "EMAIL_CAP"),
        ({"email_aliases": "a@x.com,A@x.com"}, "twice"),
        ({"email_aliases": "not-an-address"}, "comma-separated"),
        ({"ses_sender": ""}, "SES_SENDER"),
        ({"ses_max_per_second": "0"}, "SES_MAX_PER_SECOND"),
        ({"ses_max_per_second": "fast"}, "SES_MAX_PER_SECOND"),
    ],
)
def test_unsafe_email_settings_record_nothing(
    env: Conns, setting: dict[str, str], reason: str
) -> None:
    admin, app = env
    batch_id, _ = matched_batch(admin, app, 20, 46)
    configure(admin, email_mode="test_aliases", email_cap="2", email_aliases=",".join(ALIASES))
    configure(admin, **setting)
    out = run_dicts(app, "notify/plan_notifications.sql", batch_id, "t")[0]
    assert reason in out["config_error"]
    assert (out["users_with_matches"], out["emails"], out["batch_status"]) == (0, [], None)
    assert notifications(admin, batch_id) == []
    assert batch_status(admin, batch_id) == ("matched",)
