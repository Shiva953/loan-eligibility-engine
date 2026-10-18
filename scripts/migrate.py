"""Apply ``sql/migrations/NNN_*.sql`` in order, once each, then set application role passwords.

Why a tiny runner instead of a framework: the same plain SQL files must run on local Postgres
and on RDS, and a reviewer should be able to read every statement. The runner adds only:
  * a ``schema_migrations`` ledger with a SHA-256 per file, so editing an applied migration is
    an error rather than silent drift;
  * an advisory lock, so two runners can't interleave;
  * role passwords from ``.env`` (never stored in SQL). On RDS, ``lambda_app`` gets no password
    because it authenticates with IAM tokens (docs/DECISIONS.md AD1).

Usage:
    uv run python scripts/migrate.py                 # local app-db
    uv run python scripts/migrate.py --status
    uv run python scripts/migrate.py --target rds
"""

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from scripts.dbconn import Target, admin_connect_kwargs
from scripts.envfile import REPO_ROOT, load_settings, require

MIGRATIONS_DIR = REPO_ROOT / "sql" / "migrations"
FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
# Any constant works; it only has to be the same for every runner.
ADVISORY_LOCK_KEY = 7_340_001


@dataclass(frozen=True)
class Migration:
    """One migration file and the checksum that pins its applied content."""

    version: str
    path: Path
    sha256: str

    @property
    def sql_text(self) -> str:
        """Return the file's SQL."""
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """List migrations in version order, rejecting badly named files and duplicate versions."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise SystemExit(f"bad migration filename (want NNN_name.sql): {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        migrations.append(Migration(version=match.group(1), path=path, sha256=digest))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise SystemExit(f"duplicate migration versions in {directory}")
    return migrations


def ensure_ledger(conn: psycopg.Connection) -> None:
    """Create the ledger table if this database has never been migrated."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     text PRIMARY KEY,
            filename    text NOT NULL,
            sha256      text NOT NULL,
            applied_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied_checksums(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    """Map each applied version to its recorded filename and checksum."""
    rows = conn.execute("SELECT version, filename, sha256 FROM schema_migrations").fetchall()
    return {version: (filename, digest) for version, filename, digest in rows}


def apply_pending(conn: psycopg.Connection, migrations: list[Migration]) -> list[str]:
    """Apply every migration not yet in the ledger, each in its own transaction.

    Fails before applying anything if an applied file's content changed, because then the
    database no longer matches what the repository says it contains.
    """
    applied = applied_checksums(conn)
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded and recorded[1] != migration.sha256:
            raise SystemExit(
                f"{migration.path.name} changed after it was applied "
                f"(recorded {recorded[1][:12]}, now {migration.sha256[:12]}). "
                "Write a new migration instead of editing an applied one."
            )

    newly_applied: list[str] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        with conn.transaction():
            conn.execute(migration.sql_text.encode())
            conn.execute(
                "INSERT INTO schema_migrations (version, filename, sha256) VALUES (%s, %s, %s)",
                (migration.version, migration.path.name, migration.sha256),
            )
        newly_applied.append(migration.path.name)
    return newly_applied


def set_role_passwords(conn: psycopg.Connection, settings: dict[str, str], target: Target) -> None:
    """Give the login roles their passwords from .env; on RDS lambda_app uses IAM instead."""
    if target == "local":
        passwords = {
            "n8n_app": require(settings, "N8N_APP_DB_PASSWORD")[0],
            "lambda_app": require(settings, "LAMBDA_APP_DB_PASSWORD")[0],
        }
    else:
        passwords = {"n8n_app": require(settings, "RDS_N8N_APP_PASSWORD")[0]}
    for role, password in passwords.items():
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )


def run(
    target: Target,
    database: str | None = None,
    directory: Path = MIGRATIONS_DIR,
    status_only: bool = False,
) -> list[str]:
    """Migrate one database and return the filenames applied (empty when up to date)."""
    settings = load_settings()
    migrations = discover(directory)
    kwargs = admin_connect_kwargs(settings, target, database)
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            ensure_ledger(conn)
            if status_only:
                applied = applied_checksums(conn)
                for migration in migrations:
                    state = "applied" if migration.version in applied else "PENDING"
                    print(f"  {state:8} {migration.path.name}")
                return []
            newly_applied = apply_pending(conn, migrations)
            set_role_passwords(conn, settings, target)
            return newly_applied
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--target", choices=["local", "rds"], default="local")
    parser.add_argument("--database", help="override the database name (tests use this)")
    parser.add_argument("--status", action="store_true", help="list migrations and exit")
    args = parser.parse_args(argv)

    applied = run(args.target, args.database, status_only=args.status)
    if not args.status:
        if applied:
            for name in applied:
                print(f"applied {name}")
        else:
            print("database is up to date")
        print("role passwords set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
