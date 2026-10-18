"""Create or complete ``.env``: generate missing local secrets and add new template keys.

Why it never overwrites: ``N8N_ENCRYPTION_KEY`` must stay fixed once n8n has stored
credentials, and database passwords must match what the volumes were initialised with. So a
value that is already set, even a weak one, is left alone. Only blank secrets are filled in.

Usage:
    uv run python scripts/make_env.py            # .env next to .env.example
"""

import argparse
import secrets
import shutil
import sys
from pathlib import Path

from scripts.envfile import REPO_ROOT, parse_env_text

# Secrets this script may generate. User-supplied credentials (API keys, AWS keys) are not
# listed: they can only come from the owner.
GENERATED_SECRETS: tuple[str, ...] = (
    "N8N_ENCRYPTION_KEY",
    "N8N_RUNNERS_AUTH_TOKEN",
    "N8N_DB_PASSWORD",
    "APP_DB_ADMIN_PASSWORD",
    "N8N_APP_DB_PASSWORD",
    "LAMBDA_APP_DB_PASSWORD",
    "WEBHOOK_HMAC_SECRET",
    "RDS_MASTER_PASSWORD",
    "RDS_N8N_APP_PASSWORD",
)


def complete_env(env_path: Path, example_path: Path) -> tuple[list[str], list[str]]:
    """Fill blank generated secrets and append keys that exist only in the template.

    Returns the names of the generated secrets and of the appended keys. Values are never
    printed or returned.
    """
    if not env_path.exists():
        shutil.copyfile(example_path, env_path)
    env_path.chmod(0o600)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    generated: list[str] = []
    for index, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if sep and key in GENERATED_SECRETS and value.strip() == "":
            lines[index] = f"{key}={secrets.token_hex(32)}"
            generated.append(key)

    present = parse_env_text("\n".join(lines))
    template = parse_env_text(example_path.read_text(encoding="utf-8"))
    added = [key for key in template if key not in present]
    if added:
        lines.append("")
        lines.append("# --- Added by make env from .env.example ---")
        for key in added:
            value = secrets.token_hex(32) if key in GENERATED_SECRETS else template[key]
            lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return generated, added


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and report what changed, by key name only."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--example", type=Path, default=REPO_ROOT / ".env.example")
    args = parser.parse_args(argv)

    generated, added = complete_env(args.env, args.example)
    print(f"{args.env}: generated {len(generated)} secret(s), added {len(added)} key(s)")
    for key in generated:
        print(f"  generated {key}")
    for key in added:
        print(f"  added     {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
