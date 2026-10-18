#!/usr/bin/env bash
# Import (or re-import) the n8n credentials for a target database: local | rds.
#
# The JSON is built from .env by scripts/n8n_credentials.py and streamed into the container.
# It exists in plaintext only inside the container, in a umask-077 temp file that is removed
# on every exit path, and never in the repository. Fixed credential IDs mean a re-import
# overwrites in place, so workflows never need changing (scripts/n8n_ids.py).
set -euo pipefail

target="${1:-local}"
cd "$(dirname "$0")/.."

PYTHONPATH=.:services uv run --frozen python scripts/n8n_credentials.py --target "$target" \
  | docker compose exec -T n8n sh -c '
      umask 077
      f=$(mktemp)
      trap "rm -f \"$f\"" EXIT
      cat > "$f"
      n8n import:credentials --input="$f" 2>&1 | grep -v "license SDK\|migration lock"
    '
