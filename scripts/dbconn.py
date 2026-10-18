"""Connection settings for the application database, local or RDS, as the admin role.

Why one module: migrations, config seeding and the bench all need the same target switch, and
the RDS branch must always verify TLS against the AWS CA bundle (docs/DECISIONS.md AD10).
"""

from typing import Any, Literal

from scripts.envfile import REPO_ROOT, require

Target = Literal["local", "rds"]


def admin_connect_kwargs(
    settings: dict[str, str], target: Target, database: str | None = None
) -> dict[str, Any]:
    """Build psycopg ``connect`` keyword arguments for the admin (migration) role."""
    if target == "local":
        user, password, port = require(
            settings, "APP_DB_ADMIN_USER", "APP_DB_ADMIN_PASSWORD", "APP_DB_HOST_PORT"
        )
        return {
            "host": "127.0.0.1",
            "port": int(port),
            "dbname": database or settings.get("APP_DB_NAME", "loans"),
            "user": user,
            "password": password,
            "connect_timeout": 5,
        }

    host, user, password, bundle = require(
        settings, "RDS_HOST", "RDS_MASTER_USER", "RDS_MASTER_PASSWORD", "RDS_CA_BUNDLE"
    )
    return {
        "host": host,
        "port": int(settings.get("RDS_PORT", "5432")),
        "dbname": database or settings.get("RDS_DB_NAME", "loans"),
        "user": user,
        "password": password,
        "sslmode": "verify-full",
        "sslrootcert": str(REPO_ROOT / bundle),
        "connect_timeout": 10,
    }
