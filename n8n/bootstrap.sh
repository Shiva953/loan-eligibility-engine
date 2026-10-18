#!/usr/bin/env bash
# Bootstrap the local stack into a working state with one command: `make bootstrap`.
#
# Safe to re-run. Every step is idempotent: migrations skip applied files, app_config is
# replaced, and credentials and workflows are imported under fixed IDs so they overwrite in
# place.
#
# Order matters:
#   1. app-db is migrated and app_config seeded, so the workflows have data to read.
#   2. Credentials are imported before workflows, because nodes reference them by ID.
#   3. Workflows are imported, then published. n8n 2.x imports everything unpublished, and CLI
#      publishing only takes effect after a restart, hence step 4.
#   4. n8n is restarted and we wait for it to be healthy, then until it serves the new versions:
#      for a few seconds after a restart it still runs the previously published one (V28).
# Importing works before anyone has created the n8n owner account (checked on 2.43.6).
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "no .env: run 'make env' first" >&2; exit 1; }

compose() { docker compose "$@"; }
py() { PYTHONPATH=.:services uv run --frozen python "$@"; }
n8n_cli() { compose exec -T n8n n8n "$@" 2>&1 | grep -v "license SDK\|migration lock" || true; }

echo "==> starting containers"
compose up -d --wait n8n-db app-db n8n n8n-runners webhook-gw fixtures

echo "==> migrating app-db and seeding app_config"
py scripts/migrate.py --target local
py scripts/seed_config.py --target local
py scripts/seed_sources.py --target local

echo "==> building workflow JSON from n8n/src"
py scripts/build_workflows.py

echo "==> importing credentials (streamed into the container, never written to the repo)"
./n8n/import-credentials.sh local

echo "==> importing workflows"
n8n_cli import:workflow --separate --input=/repo/workflows

echo "==> publishing workflows"
for id in $(py -c 'from scripts.n8n_ids import WORKFLOWS; print(" ".join(WORKFLOWS.values()))'); do
  n8n_cli publish:workflow --id="$id" | grep -i "publishing" || true
done

echo "==> restarting n8n so the published versions go live"
compose restart n8n >/dev/null
compose up -d --wait n8n n8n-runners >/dev/null 2>&1

ids=$(py -c 'from scripts.n8n_ids import WORKFLOWS; print(",".join(f"\x27{i}\x27" for i in WORKFLOWS.values()))')
stale_sql="SELECT count(*) FROM workflow_entity w LEFT JOIN workflow_published_version p ON p.\"workflowId\" = w.id WHERE w.id IN (${ids}) AND p.\"publishedVersionId\" IS DISTINCT FROM w.\"activeVersionId\""
for _ in $(seq 1 60); do
  stale=$(compose exec -T n8n-db psql -U n8n -d n8n -Atc "$stale_sql")
  [[ "$stale" == "0" ]] && break
  sleep 1
done
[[ "$stale" == "0" ]] || { echo "n8n still serves an older version of ${stale} workflow(s) after 60 s" >&2; exit 1; }

# The task runner starts its JavaScript process on the first Code node after a restart, which
# made the first batch wait 1.1-1.9 s. One signed webhook for a batch that doesn't exist runs
# Workflow B's first Code node and nothing else (the claim finds no batch), so it starts now.
echo "==> warming the Code-node runner (a no-op Workflow B run)"
py scripts/webhook_test.py >/dev/null 2>&1 || echo "  (runner warm-up webhook failed; the first batch starts it)"

port=$(py -c 'from scripts.envfile import load_settings; print(load_settings().get("N8N_HOST_PORT", "5678"))')
echo
echo "n8n is ready at http://localhost:${port} (first visit: create the owner account)"
n8n_cli list:workflow | sed 's/^/  workflow /'
