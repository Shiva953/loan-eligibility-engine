"""scripts/demo_reset.py on a scratch database.

It must undo exactly the guards that make a repeat run of each workflow take its short path,
and nothing else. The full paths themselves were checked through n8n (docs/TESTING.md,
"Demo reset").
"""

from collections.abc import Iterator

import psycopg
import pytest

from bench import benchdb
from scripts import demo_reset, migrate, seed_config
from scripts.dbconn import admin_connect_kwargs


@pytest.fixture
def conn(scratch_db: str, local_settings: dict[str, str]) -> Iterator[psycopg.Connection]:
    """A migrated scratch database with the synthetic products, everything cached."""
    migrate.run("local", scratch_db)
    seed_config.store("local", seed_config.build_config(local_settings), scratch_db)
    db = psycopg.connect(**admin_connect_kwargs(local_settings, "local", scratch_db),
                         autocommit=True)  # fmt: skip
    benchdb.load_synthetic_products(db)
    db.execute(
        "INSERT INTO crawl_sources (slug, display_name, kind, page_urls, last_content_hash) "
        "VALUES ('lender-a', 'A', 'lender', '{https://a.invalid}', 'hash-a'), "
        "       ('lender-b', 'B', 'lender', '{https://b.invalid}', 'hash-b')"
    )
    db.execute(
        "INSERT INTO llm_decisions (criteria_hash, user_signature, verdict, model, prompt_version) "
        "SELECT DISTINCT criterion_key, signature, 'uncertain', 'm', 'v' "
        "FROM cohort_space('threshold') "
        "UNION ALL SELECT 'retired-criterion', 'age=..40', 'eligible', 'm', 'v'"
    )
    db.execute(
        "INSERT INTO criterion_rules (criterion_key, criterion_text, depends_on, clauses, model, "
        "prompt_version) "
        "SELECT DISTINCT criterion_key, 'text', '{}'::text[], '[]'::jsonb, 'm', 'v' "
        "FROM cohort_space('threshold') "
        "UNION ALL SELECT 'retired-criterion', 'text', '{}', '[]', 'm', 'v'"
    )
    with db:
        yield db


def counts(db: psycopg.Connection) -> tuple[int, int, list[tuple[str, str | None]]]:
    verdicts = db.execute("SELECT count(*) FROM llm_decisions").fetchone()
    rules = db.execute("SELECT count(*) FROM criterion_rules").fetchone()
    hashes = db.execute(
        "SELECT slug, last_content_hash FROM crawl_sources WHERE enabled ORDER BY slug"
    ).fetchall()
    assert verdicts is not None
    assert rules is not None
    return int(verdicts[0]), int(rules[0]), [(str(s), h) for s, h in hashes]


def test_a_dry_run_changes_nothing(conn: psycopg.Connection, scratch_db: str) -> None:
    before = counts(conn)
    assert demo_reset.main(["--database", scratch_db, "--source", "lender-a", "--dry-run"]) == 0
    assert counts(conn) == before


def test_the_reset_forgets_one_page_and_the_active_cohorts_only(
    conn: psycopg.Connection, scratch_db: str
) -> None:
    assert demo_reset.main(["--database", scratch_db, "--source", "lender-a", "--yes"]) == 0
    # Only the retired criterion's rows stay: they are outside the active products' space.
    assert counts(conn) == (1, 1, [("lender-a", None), ("lender-b", "hash-b")])
    users = conn.execute("SELECT count(*) FROM users").fetchone()
    assert users == (0,)


def test_a_cold_batch_waits_for_b_with_its_cohorts_uncached(
    conn: psycopg.Connection, scratch_db: str
) -> None:
    assert demo_reset.main(["--database", scratch_db, "--cold-batch", "200", "--yes"]) == 0
    verdicts, rules, hashes = counts(conn)
    assert (verdicts, rules) == (1, 1)
    assert hashes == [("lender-a", "hash-a"), ("lender-b", "hash-b")]  # A's pages untouched
    batch = conn.execute(
        "SELECT b.status, count(u.*) FROM ingestion_batches b JOIN users u USING (batch_id) "
        "GROUP BY b.batch_id, b.status"
    ).fetchall()
    assert batch == [("ingested", 200)]


def test_an_unknown_source_is_refused(conn: psycopg.Connection, scratch_db: str) -> None:
    before = counts(conn)
    assert demo_reset.main(["--database", scratch_db, "--source", "nope", "--yes"]) == 1
    assert counts(conn) == before
