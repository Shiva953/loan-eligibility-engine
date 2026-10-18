# Loan Eligibility Engine: developer entry points.
# Each phase adds targets here; docs/TESTING.md lists the commands that check each phase.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV        ?= uv
# services/ is on the path so scripts share code with the Lambdas (e.g. common.webhook_jwt).
PY        := PYTHONPATH=.:services $(UV) run --frozen python
COMPOSE   := docker compose
USERS_CSV ?= data/users.csv
CORE      := n8n-db app-db n8n n8n-runners webhook-gw fixtures

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Phase 0: tooling and data profile -------------------------------------------------------

.PHONY: setup
setup: ## Create .venv from uv.lock (pinned tools and psycopg)
	$(UV) sync --frozen

.PHONY: lint
lint: ## ruff (lint + format check) and mypy --strict
	$(UV) run --frozen ruff check .
	$(UV) run --frozen ruff format --check .
	$(UV) run --frozen mypy

.PHONY: fmt
fmt: ## Auto-fix lint issues and format
	$(UV) run --frozen ruff check --fix .
	$(UV) run --frozen ruff format .

.PHONY: test
test: ## Run pytest (DB tests skip if app-db is not running)
	$(UV) run --frozen pytest

.PHONY: profile
profile: ## Profile the users CSV into docs/PROFILE.md
	$(PY) scripts/profile_users.py $(USERS_CSV) --out docs/PROFILE.md

.PHONY: profile-check
profile-check: ## Fail if docs/PROFILE.md does not match a fresh profile
	$(PY) scripts/profile_users.py $(USERS_CSV) --out docs/PROFILE.md --check

.PHONY: check
check: lint test test-js profile-check workflows-check ## Lint, tests, and generated-file freshness

# --- Phase 1: local stack --------------------------------------------------------------------

.PHONY: env
env: ## Create/complete .env: generate blank local secrets, never overwrite set values
	$(PY) scripts/make_env.py

.PHONY: up
up: ## Start the local stack (no tunnel) and wait until healthy
	$(COMPOSE) up -d --wait $(CORE)

.PHONY: down
down: ## Stop the stack (keeps data volumes)
	$(COMPOSE) --profile tunnel down

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) --profile tunnel ps

.PHONY: logs
logs: ## Follow logs (SERVICE=n8n to filter)
	$(COMPOSE) --profile tunnel logs -f $(SERVICE)

.PHONY: migrate
migrate: ## Apply SQL migrations to the local app-db
	$(PY) scripts/migrate.py --target local

.PHONY: config
config: ## Copy the non-secret switches from .env into app_config
	$(PY) scripts/seed_config.py --target local

.PHONY: build-workflows
build-workflows: ## Build n8n/workflows/*.json from n8n/src templates
	$(PY) scripts/build_workflows.py

.PHONY: workflows-check
workflows-check: ## Fail if n8n/workflows is stale relative to n8n/src, sql/ and n8n/code
	$(PY) scripts/build_workflows.py --check

.PHONY: bootstrap
bootstrap: ## Start, migrate, seed config, import credentials + workflows, publish, restart
	./n8n/bootstrap.sh

.PHONY: export-workflows
export-workflows: ## Pull workflows out of n8n into build/n8n-export; fail if they drift from the repo
	$(PY) scripts/export_workflows.py

.PHONY: use-local
use-local: ## Point n8n's app Postgres credential and app_config at the local app-db
	$(PY) scripts/seed_config.py --target local
	./n8n/import-credentials.sh local
	$(COMPOSE) restart n8n && $(COMPOSE) up -d --wait n8n n8n-runners

.PHONY: use-rds
use-rds: ## Point n8n's app Postgres credential and app_config at RDS (waits for RDS to be up)
	$(PY) scripts/stack.py ready
	$(PY) scripts/seed_config.py --target rds
	./n8n/import-credentials.sh rds
	$(COMPOSE) restart n8n && $(COMPOSE) up -d --wait n8n n8n-runners

.PHONY: tunnel
tunnel: ## Start the cloudflared quick tunnel (exposes only webhook-gw) and print its URL
	$(COMPOSE) --profile tunnel up -d cloudflared
	@for i in $$(seq 1 30); do $(PY) scripts/tunnel_url.py 2>/dev/null && exit 0; sleep 2; done; \
	  echo "tunnel did not report a URL; see 'make logs SERVICE=cloudflared'" >&2; exit 1

.PHONY: tunnel-url
tunnel-url: ## Print the current quick-tunnel URL
	$(PY) scripts/tunnel_url.py

.PHONY: tunnel-down
tunnel-down: ## Stop the tunnel
	$(COMPOSE) --profile tunnel stop cloudflared

# --- Phase 2a: Workflow A (product discovery) ----------------------------------------------

.PHONY: fixtures
fixtures: ## Re-fetch the crawl fixtures from the live sites (polite: robots.txt, UA, delays)
	$(PY) scripts/fetch_fixtures.py

.PHONY: seed-sources
seed-sources: ## Upsert data/crawl_sources.json into crawl_sources
	$(PY) scripts/seed_sources.py

.PHONY: crawl
crawl: ## Run Workflow A once and report (ARGS=--reset forces LLM re-extraction)
	$(PY) scripts/crawl.py $(ARGS)

.PHONY: crawl-report
crawl-report: ## Report on the latest Workflow A run, products and today's LLM calls
	$(PY) scripts/crawl.py --report

.PHONY: test-js
test-js: ## Run the Code-node JavaScript tests with node --test
	node --test tests/js/*.test.mjs 'ui/lib/*.test.ts'

.PHONY: webhook-test
webhook-test: ## Send a signed webhook through webhook-gw (ARGS="--mode unsigned", "--url <tunnel>")
	$(PY) scripts/webhook_test.py $(ARGS)

.PHONY: match-e2e
match-e2e: ## Workflow B end to end: load a batch, send the signed webhook, verify vs the reference (ARGS="--csv data/users.csv" or "--n 10000")
	$(PY) scripts/match_e2e.py $(ARGS)

.PHONY: ingest-local
ingest-local: ## Ingest a CSV as the ingest Lambda does, as lambda_app (FILE=data/users.csv; ARGS="--notify --wait")
	$(PY) scripts/ingest_local.py $(or $(FILE),data/users.csv) $(ARGS)

.PHONY: demo-local
demo-local: ## The whole pipeline from a CSV: ingest, signed webhook, Workflow B and C (FILE=...)
	$(PY) scripts/ingest_local.py $(or $(FILE),data/users.csv) --notify --wait

.PHONY: gen-users
gen-users: ## Generate a users CSV like the sample (ARGS="--n 100000 --dirty-rate 0.02 --out build/users-100k.csv")
	$(PY) scripts/gen_users.py $(or $(ARGS),--n 100000 --dirty-rate 0.02 --out build/users-100k.csv)

.PHONY: ui-install
ui-install: ## Install the upload page's pinned npm packages (ui/package-lock.json)
	cd ui && npm ci --no-fund --no-audit

.PHONY: ui-build
ui-build: ## Build the upload page (Next.js static export into ui/out)
	npm --prefix ui run build

.PHONY: ui
ui: ui-build ## Upload page at http://localhost:8090, with local stand-ins for presign, S3 and status
	$(PY) scripts/ui_server.py

.PHONY: ui-dev
ui-dev: ## Page with hot reload at http://localhost:3000 (next dev), API stand-ins on 8090
	trap 'kill 0' EXIT; $(PY) scripts/ui_server.py --api-only & npm --prefix ui run dev

.PHONY: load-batch
load-batch: ## Load users as an 'ingested' batch, no webhook; then "Run manually" in Workflow B (ARGS="--n 1000" default)
	$(PY) scripts/match_e2e.py --load-only $(or $(ARGS),--n 1000)

.PHONY: demo-reset
demo-reset: ## Next run of each workflow takes its full path (ARGS="--cold-batch 1000" before B, "--target rds", "--dry-run")
	$(PY) scripts/demo_reset.py $(ARGS)

.PHONY: bench
bench: ## Optimisation bench: Workflow B's funnel at 1k/10k/100k, real + wide profiles (no LLM calls)
	$(PY) bench/run.py $(ARGS)

.PHONY: bench-llm
bench-llm: ## LLM-stage strategies vs the oracle (cached in bench/llm_cache; ARGS=--holdout validates)
	$(PY) bench/llm_experiments.py $(ARGS)

.PHONY: warm-demo
warm-demo: ## Warm step through n8n on a scratch DB (2 Gemini calls), then a 10k batch with 0 calls (ARGS=--lazy: B makes them)
	$(PY) scripts/warm_demo.py $(ARGS)

.PHONY: timing
timing: ## Region RTT, DB RTT, round trips per run; in-region time vs network (ARGS="--run 10000", "--target rds")
	$(PY) scripts/timing.py $(ARGS)

.PHONY: email-preview
email-preview: ## Write the emails Workflow C rendered in its latest run to build/email-preview/
	$(PY) scripts/email_preview.py

# --- Phase 3: AWS (anything that creates, changes or removes resources: ask the owner first) ---

.PHONY: lambda-build
lambda-build: ## Build build/lambda/app.zip and deps.zip (psycopg for Lambda arm64, cp314); offline
	$(PY) scripts/build_lambda.py

.PHONY: sls-package
sls-package: lambda-build ## Compile infra/serverless.yml with osls into infra/.serverless/ (read-only AWS lookups)
	$(PY) scripts/sls.py package

.PHONY: aws-params
aws-params: ## ASK FIRST. Put the SSM parameters the stack reads (2 SecureStrings + the tunnel URL)
	$(PY) scripts/aws_params.py put

.PHONY: tunnel-ssm
tunnel-ssm: ## Write the current quick-tunnel URL to SSM (after every `make tunnel`)
	$(PY) scripts/aws_params.py tunnel

.PHONY: migrate-rds
migrate-rds: ## Apply SQL migrations to RDS (after `make deploy` and RDS_HOST in .env)
	$(PY) scripts/migrate.py --target rds

.PHONY: rds-ca
rds-ca: ## Download the public RDS CA bundle into certs/ (n8n and the Lambdas verify RDS with it)
	$(PY) -c "from scripts.build_lambda import ensure_ca_bundle; print(ensure_ca_bundle())"

.PHONY: catalog-to-rds
catalog-to-rds: ## Sync products, crawl state and LLM caches from the local app-db to RDS (upserts, never deletes)
	$(PY) scripts/copy_catalog.py

.PHONY: deploy
deploy: lambda-build ## ASK FIRST. Deploy the stack with Serverless v4, then write build/stack-outputs.json
	$(PY) scripts/sls.py deploy
	$(PY) scripts/stack.py outputs
	$(PY) scripts/stack.py warm

.PHONY: stack-outputs
stack-outputs: ## Write the stack's outputs to build/stack-outputs.json and print RDS_HOST
	$(PY) scripts/stack.py outputs

.PHONY: rds-stop
rds-stop: ## Stop RDS between sessions (it bills by the hour; AWS restarts it after 7 days)
	$(PY) scripts/stack.py stop

.PHONY: rds-start
rds-start: ## Start RDS, wait until it is available, then warm the Lambdas' logins (a few minutes)
	$(PY) scripts/stack.py start

.PHONY: aws-upload
aws-upload: ## Upload a CSV to the deployed stack as the page does, timed to the end (FILE=data/warmup.csv sends no email)
	$(PY) scripts/aws_upload.py $(or $(FILE),data/warmup.csv)

.PHONY: warm
warm: ## Log the in-VPC Lambdas in to RDS now (also run by deploy, rds-start and use-rds)
	$(PY) scripts/stack.py warm

.PHONY: rds-state
rds-state: ## Show whether RDS is running
	$(PY) scripts/stack.py state

.PHONY: destroy
destroy: ## ASK FIRST. Empty the bucket, remove the stack (RDS deleted, no snapshot), delete the SSM parameters
	$(PY) scripts/sls.py remove
	$(PY) scripts/aws_params.py delete
