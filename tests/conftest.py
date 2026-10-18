"""Shared fixtures.

Tests that need Postgres use a throwaway database on the local app-db and are skipped (not
failed) when the stack isn't running, so ``make test`` stays usable without Docker.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from scripts.dbconn import admin_connect_kwargs
from scripts.envfile import DEFAULT_ENV_FILE, load_settings


@pytest.fixture(scope="session")
def local_settings() -> dict[str, str]:
    if not DEFAULT_ENV_FILE.exists():
        pytest.skip("no .env (run `make env`)")
    return load_settings()


@pytest.fixture
def scratch_db(local_settings: dict[str, str]) -> Iterator[str]:
    """Create an empty database on the local app-db server and drop it afterwards."""
    yield from _scratch(local_settings)


@pytest.fixture
def second_scratch_db(local_settings: dict[str, str]) -> Iterator[str]:
    """A second empty database, for tests that copy from one database to another."""
    yield from _scratch(local_settings)


def _scratch(local_settings: dict[str, str]) -> Iterator[str]:
    name = f"loans_test_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(**admin_connect_kwargs(local_settings, "local"), autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("local app-db is not running (run `make up`)")
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(
            **admin_connect_kwargs(local_settings, "local"), autocommit=True
        ) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
