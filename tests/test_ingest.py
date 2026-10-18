"""Ingest (services/ingest/core.py) against a real Postgres, as lambda_app, the Lambdas' role.

The checks the upload depends on: every row is either stored or rejected with a reason, a
repeated user_id keeps its last row, columns can come in any order with extras, a BOM and CRLF,
a file without the required columns is refused as a whole, and a redelivered S3 event changes
nothing. A plain-Python reference validator decides what should happen to each row.
"""

import csv
import io
import re
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from ingest.core import COLUMNS, IngestError, analyze_users, fail, ingest
from scripts import migrate
from scripts.dbconn import admin_connect_kwargs
from scripts.gen_users import dirty, generate
from status.core import batch_status

Conns = tuple[psycopg.Connection, psycopg.Connection]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


@pytest.fixture
def env(scratch_db: str, local_settings: dict[str, str]) -> Iterator[Conns]:
    """(admin, lambda_app) connections to a migrated scratch database."""
    migrate.run("local", scratch_db)
    kwargs = admin_connect_kwargs(local_settings, "local", scratch_db)
    admin = psycopg.connect(**kwargs, autocommit=True)
    lam = psycopg.connect(
        **{**kwargs, "user": "lambda_app", "password": local_settings["LAMBDA_APP_DB_PASSWORD"]}
    )
    try:
        yield admin, lam
    finally:
        lam.close()
        admin.close()


def as_csv(
    rows: list[dict[str, str]], header: tuple[str, ...] = COLUMNS, bom: bool = False
) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("﻿" if bom else "").encode() + out.getvalue().encode()


def valid(row: dict[str, str]) -> bool:
    """The reference: the same rules as the SQL, written independently in Python."""
    try:
        income = float(row["monthly_income"].strip())
        score = int(row["credit_score"].strip())
        age = int(row["age"].strip())
    except ValueError:
        return False
    return bool(
        UUID_RE.match(row["user_id"].strip())
        and row["name"].strip()
        and EMAIL_RE.match(row["email"].strip())
        and re.fullmatch(r"[0-9]{1,10}(\.[0-9]{1,2})?", row["monthly_income"].strip())
        and income > 0
        and 300 <= score <= 900
        and row["employment_status"].strip().lower() in ("salaried", "self-employed", "business")
        and 18 <= age <= 100
    )


def run(lam: psycopg.Connection, data: bytes, key: str = "uploads/t.csv", etag: str = "e1") -> Any:
    saved: dict[str, bytes] = {}

    def save(rejects_key: str, rows: Any) -> None:
        saved[rejects_key] = rows.read()

    result = ingest(lam, io.BytesIO(data), bucket="b", key=key, etag=etag, save_rejects=save)
    return result, saved


def test_a_clean_file_is_stored_and_the_batch_closed(env: Conns) -> None:
    admin, lam = env
    users = generate(300, 1, "real")
    result, saved = run(lam, as_csv(users))
    assert (result.rows_total, result.rows_valid, result.rows_rejected) == (300, 300, 0)
    assert (result.rejects_key, saved) == (None, {})
    stored = admin.execute(
        "SELECT count(*) FROM users WHERE batch_id = %s", (result.batch_id,)
    ).fetchone()
    assert stored == (300,)
    batch = admin.execute(
        "SELECT status, ingested_at IS NOT NULL FROM ingestion_batches WHERE batch_id = %s",
        (result.batch_id,),
    ).fetchone()
    assert batch == ("ingested", True)


def test_dirty_rows_are_rejected_with_a_reason_and_the_last_repeat_wins(env: Conns) -> None:
    admin, lam = env
    rows, counts = dirty(generate(2000, 2, "real"), 0.15, 2)
    assert counts["duplicate_user_id"] > 0
    assert counts["padded_value"] > 0
    result, saved = run(lam, as_csv(rows))

    last_valid: dict[str, dict[str, str]] = {}
    for row in rows:
        if valid(row):
            last_valid[row["user_id"].strip().lower()] = row
    assert result.rows_total == len(rows)
    assert result.rows_valid == len(last_valid)
    assert result.rows_valid + result.rows_rejected == result.rows_total

    stored: dict[str, int] = dict(admin.execute(
        "SELECT user_id::text, credit_score FROM users WHERE batch_id = %s", (result.batch_id,)
    ).fetchall())  # fmt: skip
    assert stored == {uid: int(r["credit_score"]) for uid, r in last_valid.items()}

    (rejects,) = saved.values()
    reasons = [r["reason"] for r in csv.DictReader(io.StringIO(rejects.decode()))]
    assert len(reasons) == result.rows_rejected
    assert set(reasons) <= {
        "monthly_income is not a number",
        "credit_score is not a whole number",
        "age is outside 18-100",
        "employment_status is not Salaried, Self-Employed or Business",
        "email is not an address",
        "user_id repeated: a later row replaces this one",
    }
    assert reasons.count("age is outside 18-100") == counts["out_of_range"]


def test_columns_in_any_order_with_extras_a_bom_and_crlf(env: Conns) -> None:
    _, lam = env
    users = [{**u, "notes": "x, with a comma"} for u in generate(50, 3, "real")]
    header = ("age", "email", "notes", "user_id", "employment_status", "credit_score",
              "monthly_income", "name")  # fmt: skip
    result, _ = run(lam, as_csv(users, header, bom=True))
    assert (result.rows_valid, result.rows_rejected) == (50, 0)


def test_a_file_without_the_required_columns_is_refused_as_a_whole(env: Conns) -> None:
    admin, lam = env
    with pytest.raises(IngestError, match="missing column"):
        run(lam, b"user_id,name\r\nabc,def\r\n")
    batch_id = fail(lam, bucket="b", key="uploads/t.csv", etag="e1", error="missing column(s): x")
    status = batch_status(lam, key="uploads/t.csv")
    assert (status["batch_id"], status["status"], status["done"]) == (batch_id, "failed", True)
    assert admin.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_a_redelivered_event_changes_nothing(env: Conns) -> None:
    admin, lam = env
    data = as_csv(generate(40, 4, "real"))
    first, _ = run(lam, data)
    again, _ = run(lam, data)
    assert (again.duplicate, again.batch_id) == (True, first.batch_id)
    assert admin.execute("SELECT count(*) FROM ingestion_batches").fetchone() == (1,)


def test_a_re_upload_moves_its_users_to_the_new_batch(env: Conns) -> None:
    admin, lam = env
    users = generate(40, 5, "real")
    first, _ = run(lam, as_csv(users))
    changed = [{**u, "monthly_income": "123456"} for u in users]
    second, _ = run(lam, as_csv(changed), key=f"uploads/{uuid.uuid4()}.csv", etag="e2")
    rows = admin.execute(
        "SELECT batch_id::text, monthly_income, count(*) FROM users GROUP BY 1, 2"
    ).fetchall()
    assert [(b, int(m), n) for b, m, n in rows] == [(second.batch_id, 123456, 40)]
    assert first.batch_id != second.batch_id


def test_ingest_refreshes_the_users_statistics(env: Conns) -> None:
    admin, lam = env
    run(lam, as_csv(generate(30, 6, "real")))
    analyzed = admin.execute(
        "SELECT last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname = 'users'"
    ).fetchone()
    assert analyzed == (True,)  # lambda_app holds MAINTAIN on users (migration 012)


def test_status_before_ingest_and_after(env: Conns) -> None:
    _, lam = env
    assert batch_status(lam, key="uploads/none.csv") == {
        "status": "uploaded",
        "key": "uploads/none.csv",
        "done": False,
    }
    result, _ = run(lam, as_csv(generate(20, 7, "real")), key="uploads/s.csv")
    status = batch_status(lam, key="uploads/s.csv")
    assert (status["status"], status["rows"]["valid"], status["done"]) == ("ingested", 20, False)
    assert status["ms"]["ingest"] is not None
    assert status["batch_id"] == result.batch_id


@pytest.mark.parametrize(
    ("rows_valid", "reltuples", "expected"),
    [(10, -1, "ANALYZE users"), (10_000, 50_000, "ANALYZE users"),
     (10_000, 160_000, "ANALYZE users (batch_id)")],
)  # fmt: skip
def test_the_whole_table_is_analyzed_only_when_the_batch_is_a_large_share(
    rows_valid: int, reltuples: float, expected: str
) -> None:
    assert analyze_users(rows_valid, reltuples) == expected


def test_a_small_batch_still_shows_in_batch_id_statistics(env: Conns) -> None:
    admin, lam = env
    run(lam, as_csv(generate(300, 10, "real")))
    second, _ = run(lam, as_csv(generate(20, 11, "real")), key="uploads/u2.csv", etag="e2")
    (values,) = admin.execute(
        "SELECT most_common_vals::text FROM pg_stats "
        "WHERE tablename = 'users' AND attname = 'batch_id'"
    ).fetchone() or (None,)
    assert second.batch_id in str(values)  # analyzed (batch_id) only: 20 * 10 < 300 rows


INSERT_USER = (
    "INSERT INTO users (user_id, name, email, monthly_income, credit_score, employment_status, "
    "age, batch_id) VALUES (%s, 'N', 'n@example.com', 50000, 700, 'Salaried', 30, %s)"
)


def test_users_reference_their_batch_as_a_foreign_key_would(env: Conns) -> None:
    """Migration 013 replaced the row-level foreign key with one check per statement."""
    admin, lam = env
    result, _ = run(lam, as_csv(generate(5, 12, "real")))
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="missing ingestion batch"):
        admin.execute(INSERT_USER, (str(uuid.uuid4()), str(uuid.uuid4())))
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="missing ingestion batch"):
        admin.execute("UPDATE users SET batch_id = gen_random_uuid()")
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="still referenced by users"):
        admin.execute("DELETE FROM ingestion_batches WHERE batch_id = %s", (result.batch_id,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="can't change"):
        admin.execute("UPDATE ingestion_batches SET batch_id = gen_random_uuid()")
    unused = fail(lam, bucket="b", key="uploads/f.csv", etag="f", error="x")
    admin.execute("DELETE FROM ingestion_batches WHERE batch_id = %s", (unused,))

    before = admin.execute("SELECT max(updated_at) FROM users").fetchone()
    admin.execute("UPDATE users SET age = age")  # doesn't set updated_at: the trigger does
    after = admin.execute("SELECT min(updated_at) FROM users").fetchone()
    assert before is not None
    assert after is not None
    assert after[0] > before[0]


def test_a_write_in_flight_locks_its_batch_against_a_delete(env: Conns) -> None:
    admin, lam = env
    batch_id = fail(lam, bucket="b", key="uploads/l.csv", etag="l", error="x")
    lam.execute(INSERT_USER, (str(uuid.uuid4()), batch_id))  # not committed yet
    admin.execute("SET lock_timeout = '200ms'")
    with pytest.raises(psycopg.errors.LockNotAvailable):
        admin.execute("DELETE FROM ingestion_batches WHERE batch_id = %s", (batch_id,))
    lam.commit()
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="still referenced by users"):
        admin.execute("DELETE FROM ingestion_batches WHERE batch_id = %s", (batch_id,))


def test_a_gzipped_upload_is_read_like_the_plain_file(env: Conns) -> None:
    import gzip as gz

    _, lam = env
    rows_, _ = dirty(generate(400, 13, "real"), 0.1, 13)
    plain, _ = run(lam, as_csv(rows_), key="uploads/plain.csv")
    zipped, _ = run(lam, gz.compress(as_csv(rows_, bom=True)), key="uploads/zipped.csv", etag="z")
    assert (zipped.rows_total, zipped.rows_valid, zipped.rows_rejected) == (
        plain.rows_total, plain.rows_valid, plain.rows_rejected)  # fmt: skip


HEADER = b"user_id,name,email,monthly_income,credit_score,employment_status,age\r\n"


@pytest.mark.parametrize(
    "data",
    [
        b"\x1f\x8b\x08\x00broken",
        HEADER + b"\x00\x01,x\r\n",
        HEADER + b'"' + b"a" * 200_000 + b'",x\r\n',
    ],
    ids=["truncated-gzip", "nul-bytes", "giant-field"],
)
def test_an_unreadable_file_is_refused_as_a_whole(env: Conns, data: bytes) -> None:
    """Refused with a reason (so the page can say why), never retried as if it might pass."""
    admin, lam = env
    with pytest.raises(IngestError, match="not a readable CSV"):
        run(lam, data)
    assert admin.execute("SELECT count(*) FROM ingestion_batches").fetchone() == (0,)
