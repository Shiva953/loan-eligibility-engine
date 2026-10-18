"""Migrations against a real Postgres: idempotence, tamper detection, and least privilege.

The privilege tests log in as the application roles, because grants are only meaningful when
checked from the role that has them.
"""

import shutil
from pathlib import Path

import psycopg
import pytest

from scripts import migrate, seed_config
from scripts.dbconn import admin_connect_kwargs
from scripts.migrate import MIGRATIONS_DIR, discover

EXPECTED_TABLES = {
    "app_config",
    "crawl_runs",
    "crawl_sources",
    "criterion_rules",
    "ingestion_batches",
    "llm_calls",
    "llm_decisions",
    "loan_products",
    "notifications",
    "pipeline_metrics",
    "product_criteria_history",
    "schema_migrations",
    "user_matches",
    "users",
}
# Read-only views: matches presents user_matches as one row per (user, product) pair.
EXPECTED_VIEWS = {"matches"}


def connect_as(
    settings: dict[str, str], db: str, role: str, password_key: str
) -> psycopg.Connection:
    kwargs = admin_connect_kwargs(settings, "local", db)
    kwargs.update(user=role, password=settings[password_key])
    return psycopg.connect(**kwargs, autocommit=True)


def test_migration_files_are_well_formed() -> None:
    versions = [m.version for m in discover()]
    assert versions == sorted(versions)
    assert versions[0] == "001"


def test_apply_twice_is_idempotent(scratch_db: str, local_settings: dict[str, str]) -> None:
    first = migrate.run("local", scratch_db)
    assert first == [m.path.name for m in discover()]
    assert migrate.run("local", scratch_db) == []

    with psycopg.connect(**admin_connect_kwargs(local_settings, "local", scratch_db)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        views = {
            row[0]
            for row in conn.execute(
                "SELECT viewname FROM pg_views WHERE schemaname = 'public'"
            ).fetchall()
        }
    assert tables == EXPECTED_TABLES
    assert views == EXPECTED_VIEWS


def test_edited_migration_is_rejected(scratch_db: str, tmp_path: Path) -> None:
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copy)
    migrate.run("local", scratch_db, directory=copy)

    target = copy / "003_grants.sql"
    target.write_text(target.read_text() + "\n-- edited after apply\n")
    with pytest.raises(SystemExit, match="changed after it was applied"):
        migrate.run("local", scratch_db, directory=copy)


def test_generated_and_check_columns(scratch_db: str, local_settings: dict[str, str]) -> None:
    migrate.run("local", scratch_db)
    with psycopg.connect(**admin_connect_kwargs(local_settings, "local", scratch_db)) as conn:
        source_id = conn.execute(
            "INSERT INTO crawl_sources (slug, display_name, kind, page_urls) "
            "VALUES ('t', 'T', 'lender', ARRAY['https://example.com']) RETURNING source_id"
        ).fetchone()
        assert source_id is not None
        fuzzy = conn.execute(
            """
            INSERT INTO loan_products (source_id, source_url, lender, product_name, soft_criteria,
                                       criteria_json, criteria_hash, content_hash)
            VALUES (%s, 'https://example.com', 'L', 'P',
                    '[{"text": "x", "class": "fuzzy", "depends_on": ["employment_status"]}]',
                    '{}', 'h', 'c')
            RETURNING has_fuzzy_criteria
            """,
            (source_id[0],),
        ).fetchone()
        assert fuzzy == (True,)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE loan_products SET allowed_employment = ARRAY['Student']")


def test_role_privileges(scratch_db: str, local_settings: dict[str, str]) -> None:
    migrate.run("local", scratch_db)
    seed_config.store("local", seed_config.build_config(local_settings), scratch_db)

    with connect_as(local_settings, scratch_db, "n8n_app", "N8N_APP_DB_PASSWORD") as n8n:
        config = n8n.execute("SELECT count(*) FROM app_config").fetchone()
        assert config is not None
        assert config[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            n8n.execute("INSERT INTO users (user_id) VALUES (gen_random_uuid())")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            n8n.execute("UPDATE app_config SET value = 'test_aliases' WHERE key = 'email_mode'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            n8n.execute("DELETE FROM user_matches")

    with connect_as(local_settings, scratch_db, "lambda_app", "LAMBDA_APP_DB_PASSWORD") as lam:
        lam.execute("CREATE TEMP TABLE staging (x int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            lam.execute("SELECT * FROM loan_products")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            lam.execute("SELECT * FROM app_config")
