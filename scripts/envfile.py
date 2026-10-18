"""Read the repo's ``.env`` the same way Docker Compose does, without extra dependencies.

Why not python-dotenv: the scripts only need ``KEY=VALUE`` lines. Following Compose's rules
(process environment wins over the file) keeps the scripts and the containers seeing the same
values for the same variable.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, ignoring blanks and ``#`` comments.

    A value wrapped in matching single or double quotes is unwrapped. Inline comments are not
    stripped, because values such as the crawler User-Agent may legitimately contain ``#``-free
    punctuation and quoting is the documented way to protect anything unusual.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def read_env_file(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Read one env file. A missing file yields an empty mapping, so callers can say what to run."""
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


def load_settings(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Merge the env file with the process environment, which wins (as in Docker Compose)."""
    settings = read_env_file(path)
    for key in settings:
        if key in os.environ:
            settings[key] = os.environ[key]
    return settings


def require(settings: dict[str, str], *keys: str) -> list[str]:
    """Return the values of ``keys``, failing with one message that names every missing key."""
    missing = [key for key in keys if not settings.get(key)]
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)} (run `make env`?)")
    return [settings[key] for key in keys]
