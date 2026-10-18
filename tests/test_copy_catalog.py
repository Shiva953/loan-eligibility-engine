"""scripts/copy_catalog.py: the catalogue sync from the local app-db to RDS, on two scratch DBs.

RDS keeps match rows that reference its products, so the sync must never delete: it upserts by
primary key, carries retirements over as is_active = false, and is safe to run again.
"""

from collections.abc import Iterator

import psycopg
import pytest

from bench import benchdb
from scripts import copy_catalog, migrate
from scripts.dbconn import admin_connect_kwargs

Pair = tuple[psycopg.Connection, psycopg.Connection]


@pytest.fixture
def pair(scratch_db: str, second_scratch_db: str, local_settings: dict[str, str]) -> Iterator[Pair]:
    """(source, target): both migrated; the source holds the synthetic products."""
    for db in (scratch_db, second_scratch_db):
        migrate.run("local", db)
    source = psycopg.connect(**admin_connect_kwargs(local_settings, "local", scratch_db),
                             autocommit=True)  # fmt: skip
    target = psycopg.connect(**admin_connect_kwargs(local_settings, "local", second_scratch_db),
                             autocommit=True)  # fmt: skip
    benchdb.load_synthetic_products(source)
    try:
        yield source, target
    finally:
        source.close()
        target.close()


def catalogue(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT product_id, lender, product_name, min_credit_score, is_active, has_criteria "
        "FROM loan_products ORDER BY product_id"
    ).fetchall()


def test_the_sync_copies_everything_and_is_safe_to_repeat(pair: Pair) -> None:
    source, target = pair
    counts = copy_catalog.sync(source, target)
    assert counts["loan_products"] > 0
    assert catalogue(target) == catalogue(source)
    copy_catalog.sync(source, target)  # again: nothing duplicated
    assert catalogue(target) == catalogue(source)


def test_changes_and_retirements_arrive_and_nothing_referenced_is_deleted(pair: Pair) -> None:
    source, target = pair
    copy_catalog.sync(source, target)
    first, second = (r[0] for r in source.execute(
        "SELECT product_id FROM loan_products ORDER BY product_id LIMIT 2").fetchall())  # fmt: skip
    # A match on the target references the first product, as RDS's own batches do.
    user = {"user_id": "00000000-0000-4000-8000-000000000001", "name": "A",
            "email": "a@example.com", "monthly_income": "50000", "credit_score": "760",
            "employment_status": "Salaried", "age": "30"}  # fmt: skip
    batch = benchdb.load_batch(target, [user], "sync-test")
    target.execute(
        "INSERT INTO user_matches (user_id, batch_id, product_ids, statuses) VALUES "
        "('00000000-0000-4000-8000-000000000001', %s, ARRAY[%s::bigint], ARRAY['matched'])",
        (batch, first),
    )
    source.execute("UPDATE loan_products SET is_active = false WHERE product_id = %s", (first,))
    source.execute(
        "UPDATE loan_products SET min_credit_score = 750 WHERE product_id = %s", (second,)
    )
    copy_catalog.sync(source, target)
    assert catalogue(target) == catalogue(source)
    new = target.execute(
        "INSERT INTO loan_products (source_id, source_url, lender, product_name, criteria_json, "
        "criteria_hash, content_hash) "
        "SELECT source_id, 'u', 'New Lender', 'New Loan', '{}', 'h', 'c' "
        "FROM crawl_sources LIMIT 1 RETURNING product_id"
    ).fetchone()
    assert new is not None
    highest = source.execute("SELECT max(product_id) FROM loan_products").fetchone()
    assert highest is not None
    assert new[0] > highest[0]  # the sequence moved past the copied ids
