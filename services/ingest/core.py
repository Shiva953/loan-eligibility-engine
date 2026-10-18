"""Load one uploaded CSV of users into Postgres: one transaction, set-based validation.

Why this shape: an upload can be ~700k rows (the 50 MB presign limit), and Python per-row
validation would dominate the ingest. So Python does the least per row. It reads the header,
picks the needed columns by name (any order; extra columns ignored; short rows padded), and
streams the raw text into COPY. One SQL statement then validates every row in C, keeps the
last row of a repeated user_id, upserts the users, and records why each rejected row was
rejected. A redelivered S3 event finds its (bucket, key, etag) batch and does nothing.

The same function runs in the ingest Lambda (rows from S3), `make ingest-local` (from a file)
and the UI's local server, so all three behave identically.
"""

import csv
import gzip
import io
import tempfile
import time
import uuid
import zlib
from collections.abc import Buffer, Callable, Iterator
from dataclasses import dataclass
from typing import IO, Any, BinaryIO, cast

import psycopg

COLUMNS = ("user_id", "name", "email", "monthly_income", "credit_score", "employment_status", "age")
# Errors that come from the file's bytes, not from the database or the network: a truncated or
# corrupt gzip, a field over csv's 128 KB limit, or NUL bytes (Postgres text can't hold them).
UNREADABLE = (EOFError, gzip.BadGzipFile, zlib.error, csv.Error, psycopg.DataError)

# One statement: validate, choose the last row per user, upsert, record rejects, close the batch.
# CASE checks in order, so a cast only runs on text the previous branch proved castable. The
# trimmed values ride along through `ranked`, so the upsert needn't look each line up again
# (98k index probes, 270 ms, at 100k rows). users_reltuples is the table's size before this
# batch, for the ANALYZE decision in ingest().
LOAD_SQL = """
WITH checked AS (
    SELECT s.line, btrim(s.user_id) AS user_id, btrim(s.name) AS name, btrim(s.email) AS email,
           btrim(s.monthly_income) AS monthly_income, btrim(s.credit_score) AS credit_score,
           lower(btrim(s.employment_status)) AS employment_status, btrim(s.age) AS age,
           CASE
               WHEN s.user_id IS NULL OR btrim(s.user_id) = '' THEN 'user_id is missing'
               WHEN btrim(s.user_id)
                    !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   THEN 'user_id is not a UUID'
               WHEN s.name IS NULL OR btrim(s.name) = '' THEN 'name is missing'
               WHEN s.email IS NULL OR btrim(s.email) !~ '^[^@\\s,]+@[^@\\s,]+\\.[^@\\s,]+$'
                   THEN 'email is not an address'
               WHEN s.monthly_income IS NULL
                    OR btrim(s.monthly_income) !~ '^[0-9]{1,10}(\\.[0-9]{1,2})?$'
                   THEN 'monthly_income is not a number'
               WHEN btrim(s.monthly_income)::numeric <= 0 THEN 'monthly_income is not positive'
               WHEN s.credit_score IS NULL OR btrim(s.credit_score) !~ '^[0-9]{1,4}$'
                   THEN 'credit_score is not a whole number'
               WHEN btrim(s.credit_score)::integer NOT BETWEEN 300 AND 900
                   THEN 'credit_score is outside 300-900'
               WHEN lower(btrim(coalesce(s.employment_status, '')))
                    NOT IN ('salaried', 'self-employed', 'self employed', 'business')
                   THEN 'employment_status is not Salaried, Self-Employed or Business'
               WHEN s.age IS NULL OR btrim(s.age) !~ '^[0-9]{1,3}$' THEN 'age is not a whole number'
               WHEN btrim(s.age)::integer NOT BETWEEN 18 AND 100 THEN 'age is outside 18-100'
           END AS reason
    FROM staging s
),
ranked AS (
    SELECT c.*, row_number() OVER (PARTITION BY lower(c.user_id) ORDER BY c.line DESC) AS rn
    FROM checked c
    WHERE c.reason IS NULL
),
loaded AS (
    INSERT INTO users AS u (user_id, name, email, monthly_income, credit_score,
                            employment_status, age, batch_id)
    SELECT lower(r.user_id)::uuid, r.name, r.email, r.monthly_income::numeric(12, 2),
           r.credit_score::smallint,
           CASE r.employment_status
               WHEN 'salaried' THEN 'Salaried' WHEN 'business' THEN 'Business'
               ELSE 'Self-Employed'
           END,
           r.age::smallint, %(batch)s
    FROM ranked r
    WHERE r.rn = 1
    ORDER BY 1
    ON CONFLICT (user_id) DO UPDATE SET
        name = EXCLUDED.name, email = EXCLUDED.email, monthly_income = EXCLUDED.monthly_income,
        credit_score = EXCLUDED.credit_score, employment_status = EXCLUDED.employment_status,
        age = EXCLUDED.age, batch_id = EXCLUDED.batch_id, updated_at = now()
    RETURNING 1
),
rejected AS (
    INSERT INTO rejected_lines (line, reason)
    SELECT line, reason FROM checked WHERE reason IS NOT NULL
    UNION ALL
    SELECT line, 'user_id repeated: a later row replaces this one' FROM ranked WHERE rn > 1
    RETURNING 1
),
counts AS (
    SELECT (SELECT count(*) FROM staging) AS rows_total,
           (SELECT count(*) FROM loaded) AS rows_valid,
           (SELECT count(*) FROM rejected) AS rows_rejected
)
UPDATE ingestion_batches b
SET status = 'ingested', rows_total = c.rows_total, rows_valid = c.rows_valid,
    rows_rejected = c.rows_rejected,
    rejects_key = CASE WHEN c.rows_rejected > 0 THEN %(rejects_key)s END,
    ingested_at = clock_timestamp()
FROM counts c
WHERE b.batch_id = %(batch)s
RETURNING b.rows_total, b.rows_valid, b.rows_rejected, b.rejects_key,
          (SELECT reltuples FROM pg_class WHERE oid = 'users'::regclass) AS users_reltuples
"""

REJECTS_SQL = """
COPY (SELECT r.line, r.reason, s.user_id, s.name, s.email, s.monthly_income, s.credit_score,
             s.employment_status, s.age
      FROM rejected_lines r JOIN staging s USING (line) ORDER BY r.line)
TO STDOUT WITH (FORMAT csv, HEADER)
"""


class IngestError(ValueError):
    """The file can't be ingested at all (for example, required columns are missing)."""


@dataclass(frozen=True)
class IngestResult:
    """What ingest did; this is also the payload the notify Lambda receives."""

    batch_id: str
    duplicate: bool
    rows_total: int = 0
    rows_valid: int = 0
    rows_rejected: int = 0
    rejects_key: str | None = None
    ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON-able form, for the Lambda destination and the local runner."""
        return dict(self.__dict__)


def header_index(header: list[str]) -> list[int]:
    """Position of each required column in this file, matched by name (case and spaces ignored)."""
    names = [h.strip().lstrip("﻿").lower() for h in header]
    missing = [c for c in COLUMNS if c not in names]
    if missing:
        raise IngestError(f"missing column(s): {', '.join(missing)}")
    return [names.index(c) for c in COLUMNS]


class _Prefixed(io.RawIOBase):
    """A stream that yields `head` and then the rest of `tail`: puts back the bytes we peeked."""

    def __init__(self, head: bytes, tail: BinaryIO) -> None:
        self._head, self._tail = head, tail

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        if self._head:
            n = min(len(view), len(self._head))
            view[:n], self._head = self._head[:n], self._head[n:]
            return n
        data = self._tail.read(len(view))
        view[: len(data)] = data
        return len(data)


def decoded(raw: BinaryIO) -> BinaryIO:
    """The CSV bytes, decompressed on the fly when the page sent the file gzipped.

    Why: from India to Sydney the upload is the slowest step, and a CSV gzips about 4x, so the
    page compresses before sending (Content-Encoding: gzip). S3 stores the bytes as sent, so
    the gzip magic number, not a header, decides; plain uploads (curl, scripts) still work.
    """
    head = raw.read(2)
    stream = io.BufferedReader(_Prefixed(head, raw), buffer_size=1 << 20)
    return cast(BinaryIO, gzip.GzipFile(fileobj=stream) if head == b"\x1f\x8b" else stream)


def rows(text: IO[str]) -> Iterator[tuple[int, list[str | None]]]:
    """(line number, the seven required fields) for every non-blank data row."""
    reader = csv.reader(text)
    try:
        index = header_index(next(reader))
    except StopIteration as error:
        raise IngestError("the file is empty") from error
    for row in reader:
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        yield reader.line_num, [row[i] if i < len(row) else None for i in index]


def analyze_users(rows_valid: int, users_reltuples: float) -> str:
    """The ANALYZE to run before Workflow B plans its queries against the new batch.

    Matching needs batch_id's statistics to include the new batch: without them the planner
    guesses a few rows and a 10k batch matched 50-75% slower. That column alone costs ~20 ms;
    the whole table costs 85-270 ms at 160k users (sorting name and email dominates). The other
    columns only shift when the batch is a large share of the table, which is when autovacuum
    would analyze them anyway (scale factor 0.1), so in that case, or when the table has never
    been analyzed (reltuples -1), analyze everything now instead of a minute later.
    """
    if users_reltuples < 0 or rows_valid * 10 > users_reltuples:
        return "ANALYZE users"
    return "ANALYZE users (batch_id)"


def ingest(
    conn: psycopg.Connection,
    raw: BinaryIO,
    *,
    bucket: str,
    key: str,
    etag: str,
    save_rejects: Callable[[str, BinaryIO], None] | None = None,
) -> IngestResult:
    """Ingest one CSV object; idempotent on (bucket, key, etag).

    save_rejects(rejects_key, file) is called with the rejects CSV when any row was rejected,
    inside the transaction, so a failed upload of the rejects file rolls the batch back.
    """
    started = time.perf_counter()
    batch_id = str(uuid.uuid4())
    rejects_key = f"rejects/{batch_id}.csv"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingestion_batches (batch_id, s3_bucket, s3_key, etag) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (s3_bucket, s3_key, etag) DO NOTHING "
            "RETURNING batch_id",
            (batch_id, bucket, key, etag),
        )
        if cur.fetchone() is None:
            row = cur.execute(
                "SELECT batch_id::text FROM ingestion_batches "
                "WHERE s3_bucket = %s AND s3_key = %s AND etag = %s",
                (bucket, key, etag),
            ).fetchone()
            return IngestResult(batch_id=str(row[0]) if row else "", duplicate=True)
        # One round trip. work_mem keeps LOAD_SQL's two sorts in memory: at 4 MB, 100k rows
        # spilled 13 MB to disk.
        cur.execute(
            "CREATE TEMP TABLE staging (line integer PRIMARY KEY, user_id text, name text, "
            "email text, monthly_income text, credit_score text, employment_status text, "
            "age text) ON COMMIT DROP; "
            "CREATE TEMP TABLE rejected_lines (line integer, reason text) ON COMMIT DROP; "
            "SET LOCAL work_mem = '64MB'"
        )
        text = io.TextIOWrapper(decoded(raw), encoding="utf-8-sig", errors="replace", newline="")
        try:
            with cur.copy(
                "COPY staging (line, user_id, name, email, monthly_income, credit_score, "
                "employment_status, age) FROM STDIN"
            ) as copy:
                for line, fields in rows(text):
                    copy.write_row((line, *fields))
        except UNREADABLE as error:
            # The bytes themselves are wrong, so a retry can't help: refuse the file with a
            # reason the page can show, instead of retrying into the dead-letter queue.
            raise IngestError(f"the file is not a readable CSV ({error})") from error
        cur.execute(LOAD_SQL, {"batch": batch_id, "rejects_key": rejects_key})
        loaded = cur.fetchone()
        assert loaded is not None
        total, valid, rejected, saved_key, users_reltuples = loaded
        if rejected and save_rejects is not None:
            # Spills to disk past 8 MB: a file of mostly bad rows stays out of memory.
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as buffer:
                with cur.copy(REJECTS_SQL) as out:
                    for chunk in out:
                        buffer.write(chunk)
                buffer.seek(0)
                save_rejects(rejects_key, buffer)  # type: ignore[arg-type]
        cur.execute(analyze_users(valid, users_reltuples))
    return IngestResult(
        batch_id=batch_id,
        duplicate=False,
        rows_total=total,
        rows_valid=valid,
        rows_rejected=rejected,
        rejects_key=saved_key,
        ms=round((time.perf_counter() - started) * 1000),
    )


def fail(conn: psycopg.Connection, *, bucket: str, key: str, etag: str, error: str) -> str:
    """Record a file that couldn't be ingested at all, so the upload page can say why."""
    batch_id = str(uuid.uuid4())
    row = conn.execute(
        "INSERT INTO ingestion_batches (batch_id, s3_bucket, s3_key, etag, status, error, "
        "failed_at) VALUES (%s, %s, %s, %s, 'failed', %s, now()) "
        "ON CONFLICT (s3_bucket, s3_key, etag) DO UPDATE SET error = EXCLUDED.error "
        "RETURNING batch_id::text",
        (batch_id, bucket, key, etag, error[:2000]),
    ).fetchone()
    conn.commit()
    return str(row[0]) if row else batch_id
