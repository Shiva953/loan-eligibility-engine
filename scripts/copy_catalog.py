"""Sync the product catalogue and LLM caches from the local app-db to RDS (make catalog-to-rds).

Why: Workflow A and the warm step run against the local app-db, and RDS needs the same products
and caches for an AWS run to match like the local one (0 LLM calls). Re-crawling on RDS would
spend Gemini calls and could extract slightly different products.

Why a sync and not a copy: RDS keeps match rows that reference its products, so its catalogue
can't be deleted and reloaded. Every row is upserted by primary key instead, in dependency order
and in one transaction: crawl_sources, crawl_runs, loan_products, product_criteria_history,
llm_decisions, criterion_rules. Nothing is deleted on RDS. A product retired locally arrives
with is_active = false. Generated columns are recomputed by RDS, identity sequences are moved
past the copied ids, and RDS is analysed afterwards. Works on an empty RDS too.

Usage:
    uv run python scripts/copy_catalog.py
"""

import sys
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import load_settings

TABLES = (
    "crawl_sources",
    "crawl_runs",
    "loan_products",
    "product_criteria_history",
    "llm_decisions",
    "criterion_rules",
)

COLUMNS_SQL = """
SELECT a.attname, a.attidentity = 'a' AS identity_always,
       coalesce(a.attnum = ANY (i.indkey), false) AS in_key,
       a.atttypid = 'jsonb'::regtype AS is_jsonb
FROM pg_attribute a
LEFT JOIN pg_index i ON i.indrelid = a.attrelid AND i.indisprimary
WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
ORDER BY a.attnum
"""


def sync_table(local: psycopg.Connection, rds: psycopg.Connection, table: str) -> int:
    """Upsert every local row of one table into RDS by primary key; return the row count."""
    cols = local.execute(COLUMNS_SQL, (table,)).fetchall()
    names = [c[0] for c in cols]
    keys = [c[0] for c in cols if c[2]]
    overriding = any(c[1] for c in cols)
    rows: list[tuple[Any, ...]] = local.execute(
        sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(", ").join(map(sql.Identifier, names)), sql.Identifier(table)
        )
    ).fetchall()
    if not rows:
        return 0
    updates = [n for n in names if n not in keys]
    template = "INSERT INTO {t} ({cols}) {ov} VALUES ({vals}) ON CONFLICT ({keys}) {action}"
    statement = sql.SQL(template).format(
        t=sql.Identifier(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, names)),
        ov=sql.SQL("OVERRIDING SYSTEM VALUE" if overriding else ""),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(names)),
        keys=sql.SQL(", ").join(map(sql.Identifier, keys)),
        action=sql.SQL("DO UPDATE SET {}").format(
            sql.SQL(", ").join(
                sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(n)) for n in updates
            )
        )
        if updates
        else sql.SQL("DO NOTHING"),
    )
    with rds.cursor() as cur:
        cur.executemany(statement, [_jsonable(r, [c[3] for c in cols]) for r in rows])
    return len(rows)


def _jsonable(row: tuple[Any, ...], jsonb: list[bool]) -> tuple[Any, ...]:
    """Wrap the jsonb columns' values (read as dicts and lists), so they are written as jsonb."""
    return tuple(
        Jsonb(v) if is_json and v is not None else v for v, is_json in zip(row, jsonb, strict=True)
    )


def advance_sequences(rds: psycopg.Connection) -> None:
    """Move each identity sequence past the highest copied id, so new rows don't collide."""
    for table in TABLES:
        for (column,) in rds.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attidentity <> ''",
            (table,),
        ).fetchall():
            rds.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, %s), "
                    "greatest((SELECT max({c}) FROM {t}), 1))"
                ).format(c=sql.Identifier(column), t=sql.Identifier(table)),
                (table, column),
            )


def sync(local: psycopg.Connection, rds: psycopg.Connection) -> dict[str, int]:
    """Upsert every catalogue table into `rds` in one transaction; rows synced per table."""
    with rds.transaction():
        counts = {table: sync_table(local, rds, table) for table in TABLES}
        advance_sequences(rds)
    return counts


def main() -> int:
    """Sync the catalogue to RDS and print what RDS now holds."""
    settings = load_settings()
    with (
        psycopg.connect(**admin_connect_kwargs(settings, "local")) as local,
        psycopg.connect(**admin_connect_kwargs(settings, "rds")) as rds,
    ):
        for table, count in sync(local, rds).items():
            print(f"  {table}: {count} rows synced")
        rds.execute("ANALYZE")
        row = rds.execute(
            "SELECT count(*) FILTER (WHERE is_active), count(*) FILTER (WHERE is_active AND "
            "has_criteria) FROM loan_products"
        ).fetchone()
        if row:
            print(f"RDS now has {row[0]} active products, {row[1]} matchable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
