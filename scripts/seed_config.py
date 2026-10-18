"""Copy the non-secret switches from ``.env`` into the ``app_config`` table (docs/DECISIONS.md AD4).

Why a table: n8n 2.x blocks ``$env`` in workflows by default, and opening it would also expose
``N8N_ENCRYPTION_KEY`` to expressions. Workflows read ``app_config`` in their first node instead.

The table is replaced as a whole on every run, so it always mirrors ``.env`` exactly. Values are
validated here, before any workflow can act on a bad one. The email switches matter most: the
cap must be at least 1 and no more than the number of verified aliases.

Usage:
    uv run python scripts/seed_config.py [--target local|rds]
"""

import argparse
import re
import sys

import psycopg

from scripts.dbconn import Target, admin_connect_kwargs
from scripts.envfile import load_settings

# .env variable -> app_config key. Only non-secret values may appear here.
CONFIG_KEYS: dict[str, str] = {
    "EMAIL_MODE": "email_mode",
    "EMAIL_CAP": "email_cap",
    "EMAIL_ALIASES": "email_aliases",
    "SES_SENDER": "ses_sender",
    "SES_MAX_PER_SECOND": "ses_max_per_second",
    "AWS_REGION": "aws_region",
    "CRAWL_MODE": "crawl_mode",
    "CRAWL_USER_AGENT": "crawl_user_agent",
    "CRAWL_DELAY_SECONDS": "crawl_delay_seconds",
    "PRODUCT_STALE_DAYS": "product_stale_days",
    "EXTRACTION_CONFIDENCE_MIN": "extraction_confidence_min",
    "COHORT_BANDS": "cohort_bands",
    "LLM_BATCH_K": "llm_batch_k",
    "MATCH_LLM_ENABLED": "match_llm_enabled",
    "MATCH_RULES_ENABLED": "match_rules_enabled",
    "SWEEPER_STALE_MINUTES": "sweeper_stale_minutes",
    "LLM_PROVIDER": "llm_provider",
    "LLM_MODEL_EXTRACT": "llm_model_extract",
    "LLM_MODEL_COMPARE": "llm_model_compare",
    "LLM_MODEL_MATCH": "llm_model_match",
    "LLM_RPM": "llm_rpm",
    "LLM_RPD": "llm_rpd",
    "LLM_CONCURRENCY": "llm_concurrency",
    "LLM_MAX_RETRIES": "llm_max_retries",
    "WEBHOOK_MAX_SKEW_SECONDS": "webhook_max_skew_seconds",
}
SECRET_MARKERS = ("KEY", "SECRET", "PASSWORD", "TOKEN")
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
CHOICES: dict[str, set[str]] = {
    "EMAIL_MODE": {"suppressed", "test_aliases"},
    "CRAWL_MODE": {"fixtures", "live"},
    "COHORT_BANDS": {"threshold", "coarse", "fine"},
    "MATCH_LLM_ENABLED": {"true", "false"},
    "MATCH_RULES_ENABLED": {"true", "false"},
    "LLM_PROVIDER": {"gemini", "groq", "openrouter"},
}
POSITIVE_INTS = (
    "SES_MAX_PER_SECOND",
    "CRAWL_DELAY_SECONDS",
    "PRODUCT_STALE_DAYS",
    "LLM_BATCH_K",
    "SWEEPER_STALE_MINUTES",
    "LLM_RPM",
    "LLM_RPD",
    "LLM_CONCURRENCY",
    "LLM_MAX_RETRIES",
    "WEBHOOK_MAX_SKEW_SECONDS",
)


def build_config(settings: dict[str, str]) -> dict[str, str]:
    """Validate the switches and return the rows to store, failing with every problem at once."""
    if any(marker in env_key for env_key in CONFIG_KEYS for marker in SECRET_MARKERS):
        raise SystemExit("refusing to copy a secret-looking variable into app_config")

    problems: list[str] = []
    missing = [key for key in CONFIG_KEYS if not settings.get(key)]
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)} (run `make env`?)")

    for key, allowed in CHOICES.items():
        if settings[key] not in allowed:
            problems.append(f"{key}={settings[key]!r} must be one of {sorted(allowed)}")
    for key in POSITIVE_INTS:
        if not settings[key].isdigit() or int(settings[key]) < 1:
            problems.append(f"{key}={settings[key]!r} must be a positive integer")

    aliases = [a.strip() for a in settings["EMAIL_ALIASES"].split(",") if a.strip()]
    bad_aliases = [a for a in aliases if not EMAIL_RE.fullmatch(a)]
    if not aliases or bad_aliases:
        problems.append(f"EMAIL_ALIASES has invalid addresses: {bad_aliases or 'none given'}")
    cap = settings["EMAIL_CAP"]
    if not cap.isdigit() or not 1 <= int(cap) <= len(aliases):
        problems.append(f"EMAIL_CAP={cap!r} must be between 1 and {len(aliases)} (alias count)")
    if not EMAIL_RE.fullmatch(settings["SES_SENDER"]):
        problems.append(f"SES_SENDER={settings['SES_SENDER']!r} is not an email address")
    try:
        if not 0.0 <= float(settings["EXTRACTION_CONFIDENCE_MIN"]) <= 1.0:
            raise ValueError
    except ValueError:
        problems.append("EXTRACTION_CONFIDENCE_MIN must be a number between 0 and 1")

    if problems:
        raise SystemExit("invalid .env:\n  " + "\n  ".join(problems))

    config = {config_key: settings[env_key] for env_key, config_key in CONFIG_KEYS.items()}
    config["email_aliases"] = ",".join(aliases)
    return config


def store(target: Target, config: dict[str, str], database: str | None = None) -> None:
    """Replace app_config with ``config`` in one transaction."""
    settings = load_settings()
    with psycopg.connect(**admin_connect_kwargs(settings, target, database)) as conn:
        conn.execute("DELETE FROM app_config")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO app_config (key, value) VALUES (%s, %s)", sorted(config.items())
            )


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--target", choices=["local", "rds"], default="local")
    parser.add_argument("--database", help="override the database name (tests use this)")
    args = parser.parse_args(argv)

    config = build_config(load_settings())
    store(args.target, config, args.database)
    print(f"app_config: {len(config)} keys stored (email_mode={config['email_mode']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
