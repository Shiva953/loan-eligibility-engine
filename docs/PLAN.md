# Loan Eligibility Engine: build plan

Status: approved 2026-10-16. This file is the source of truth for scope and phase order. Requirements live in `docs/ASSIGNMENT.md`, and each architecture change's rationale lives in `docs/DECISIONS.md`.

## 1. Fixed decisions
- **Region** `ap-southeast-2` (Sydney), set once as `AWS_REGION` and read from there everywhere: this account's simplified sign-up pins it to one region, and moving needs the Paid plan (DECISIONS AD27). **IaC:** Serverless Framework v4 using the owner's free login. `serverless.yml` stays osls-compatible (no v4-only syntax), and the README says so.
- **Runtimes:**
  - n8n and `n8nio/runners` both pinned to **2.43.6** (the tags must match).
  - Lambda **python3.14 arm64**, with **psycopg 3.3.6** binary wheels (`cp314 manylinux_2_28_aarch64`) installed via `uv pip install --python-platform aarch64-manylinux_2_28 --only-binary :all:`.
  - cloudflared **2026.10.1**.
  - Postgres pinned to RDS's default major version, checked in phase 1.
- **LLM:** Gemini through one gateway sub-workflow. Extraction uses `gemini-3.5-flash-lite` and comparisons use `gemini-3.8-flash`. Limits default to **10 RPM / 500 RPD** in `app_config`; the owner will send the real AI Studio numbers later.
- **Email:** sender `shivaset2@gmail.com`. **`EMAIL_CAP=2`**, aliases **`shivaset2+1@gmail.com`, `shivaset2+2@gmail.com`**, all three verified in SES ap-southeast-2. Mail is sent only when `EMAIL_MODE=test_aliases`. gmail.com DMARC is `p=none`, so mail may land in Spam; the docs will say so.
- **Crawl sources:** BankBazaar + HDFC Bank + Kotak, with Hero FinCorp as backup. `CRAWL_MODE=fixtures` is the default for tests and the demo.
- **Bench headline:** "pairs needing an LLM" against naive. Real-data numbers come first, with a secondary run on `gen_users.py --profile wide`. The real data is narrow (income ≥25k, score ≥650, age 22–50), so the hard filter removes relatively few pairs there.
- **Golden set:** I draft it from the fixtures, then **stop for the owner to review**. E1 runs only afterwards, and EXPERIMENTS.md notes it was human-verified.
- **Always ask before:** `serverless deploy`/`remove`, creating any AWS resource, sending any email, calling a paid API, deleting data.

## 2. Data findings (`data/users.csv`)
- 10,000 rows, UTF-8, no BOM, **CRLF**, 7 columns: `user_id,name,email,monthly_income,credit_score,employment_status,age`. **0 dirty rows.**
- `user_id` is UUIDv4 and unique, so it's the primary key.
- **`email` is not unique**: 2,632 distinct across 10k users, up to 12 users per email. `name` isn't in the brief's list; I keep it for the greeting.
- Income: 25k–150k in steps of 1k. Score: 650–800 in steps of 5. Age: 22–50. Employment is exactly {Salaried, Self-Employed, Business}. All uniform and independent.
- Full-profile tuples are 9,869 distinct, so cohorts must be built per criterion (`depends_on`): an employment-only fuzzy criterion gives 3 cohorts per product.
- `gen_users.py --dirty-rate` injects bad rows (missing or non-numeric values, out of range, unknown employment, bad email, duplicate id, BOM, extra columns) because the real file has none.

## 3. Architecture (all changes accepted; one DECISIONS.md entry each)
These go on top of the kickoff architecture (presign → S3 → in-VPC ingest → notify outside the VPC → n8n through cloudflared; status Lambda; public RDS restricted to my IP and the Lambda security group).

1. **RDS IAM auth for in-VPC Lambdas.** Without a NAT gateway or interface endpoint they can't reach SSM, and the token is an offline SigV4 presign. DB roles: `lambda_app` (rds_iam), `n8n_app` (password, least privilege), master for migrations only. Locally `DB_AUTH=password`.
2. **Lambda Destinations instead of the S3 marker.** `ingest` onSuccess → `notify`, onFailure → SQS DLQ. A duplicate etag returns `{duplicate:true}` and notify skips it. `make replay BATCH=` re-sends.
3. **`webhook-gw` (nginx:alpine)** sits between cloudflared and n8n and allows only `POST /webhook/<path>`. The n8n UI is bound to `127.0.0.1`.
4. **`app_config` table** seeded from `.env` by bootstrap, so `N8N_BLOCK_ENV_ACCESS_IN_NODE` stays `true`. Secrets live only in n8n credentials.
5. **`notifications` table** (user, batch, real alias, SES id, `sent|suppressed|failed`). C sends to ≤`EMAIL_CAP` users and marks the rest `suppressed` with one UPDATE, so a rerun sends nothing.
6. **Sweeper schedule on Workflow B** for batches stuck in `ingested`. It uses the same idempotent path as the webhook.
7. **Worktree isolation** through `COMPOSE_PROJECT_NAME` plus port offsets: each worktree gets its own `app-db`.
8. **Cost and abuse guards:**
   - Lifecycle rules expire `uploads/` and `rejects/` after 7 days.
   - Presigned POSTs are ≤50 MB, expire after 5 minutes, use a server-side key and require `text/csv`.
   - No reserved concurrency.
   - The public IPv4 on RDS (about $0.005/h) is accepted; RDS runs only while in use.
9. **Dedicated minimal VPC** in `serverless.yml`: two public subnets, an IGW, the S3 gateway endpoint and the security groups.
10. **Verified TLS from n8n to RDS:** the RDS CA bundle plus `NODE_EXTRA_CA_CERTS`.

n8n 2.x specifics I'm relying on:
- `NODE_FUNCTION_ALLOW_BUILTIN=crypto` goes in the runners container's `/etc/n8n-task-runners.json` `env-overrides`.
- Workflows are imported, then `publish:workflow --id`, then n8n restarts.
- Sub-workflows need an Execute Workflow Trigger and must be published.

## 4. Crawl sources (evidence gathered 2026-10-16: robots.txt parsed, honest bot UA, criteria present in the server HTML)
- **BankBazaar** `/personal-loan.html` + `/personal-loan-eligibility.html`, treated as one source with its pages concatenated before hashing. It has a 15-row rate table, about 14 lender cards (age, income, city) and a 16-row bank eligibility table (age, work experience, monthly income).
- **HDFC Bank** `www.hdfc.bank.in/personal-loan/eligibility-criteria`: age 21–60, employer category (fuzzy or unknown), work experience, ₹25k monthly net income.
- **Kotak** `www.kotak.bank.in/en/personal-banking/loans/personal-loan/eligibility.html`: age 21–60, income tiers by relationship. The most permissive tier becomes the hard filter and the tier itself becomes a caveat.
- Backup **Hero FinCorp** `/personal-loan-eligibility`: "CIBIL 725+ preferred" and residence stability.
- Rejected: Paisabazaar (Akamai 403 after about 5 requests), ICICI (criteria only in calculator JSON), IDFC First (500), Bajaj, Tata Capital, IIFL, Fibe and MoneyView (404 because the pages moved).
- Products stay unique on `(source_url, product_name)`. Email removes duplicates per lender and prefers the direct source.

## 5. Phases
**At the end of every phase:** stop, give the owner the exact commands to run the check, append them to `docs/TESTING.md` (which grows into the full manual test guide), and **never commit until the owner explicitly says so**.

**Environment switch:** `make use-local` / `make use-rds` re-point n8n's app Postgres credential (same credential ID, so the workflows don't change) and the matching `app_config` rows at either `app-db` or RDS, then restart n8n. All development and phase 4 run on `app-db`. RDS is used only for end-to-end runs and the demo, so `make rds-stop` never breaks local work.

### 0. Skeleton
- `git init`, plus `CLAUDE.md` (under 80 lines: commands, constraints, layout) and this file as `docs/PLAN.md`.
- Layout: `infra/ services/ sql/{migrations,match}/ n8n/{src,code,workflows}/ ui/ bench/{golden,results}/ scripts/ tests/fixtures/ docs/`.
- `Makefile`, `.env.example` (every variable), `.gitignore`, `pyproject.toml` (uv, ruff, mypy, pytest).
- `docs/DECISIONS.md` (the 10 changes plus version assumptions), `docs/PROFILE.md` (generated by `scripts/profile_users.py`) and `docs/TESTING.md` (starts with the phase 0 commands).
- **Check:** `make lint` passes and `make profile` reproduces §2.

### 1. Local stack
- `docker-compose.yml` with these services: `n8n`, `n8n-runners`, `n8n-db`, `app-db`, `webhook-gw` and `cloudflared`. Every port and the project name are parameterised.
- `sql/migrations/NNN_*.sql` plus `scripts/migrate.py` (`schema_migrations`), covering the full data model: `ingestion_batches`, `users`, `loan_products`, `product_criteria_history`, `crawl_sources`, `crawl_runs`, `matches`, `llm_decisions`, `pipeline_metrics`, `notifications`, `app_config`, and the DB roles.
- `n8n/bootstrap.sh`: credentials JSON with stable IDs built from `.env` (app Postgres, AWS for SES, Gemini header auth, webhook HMAC secret) → `import:credentials` → import the workflows → `publish:workflow` each → restart.
- `make export-workflows` fails on drift. `make tunnel-url` reads cloudflared `/quicktunnel`, falling back to the logs, and writes SSM only when AWS is enabled.
- To confirm in this phase: plain-object credential import gets encrypted, and import works before the owner account is set up.
- **Check:** `docker compose up -d && make bootstrap` gives a UI with the credentials and published stub workflows, and the tunnel answers only `/webhook/*`.

### 2. Thin end-to-end slice (local)
- **2a. Workflow A.** One extraction config: flash-lite, cleaned text, one pass. Save the HTML fixtures to `tests/fixtures/`.
  - Flow: Schedule + Manual → `crawl_sources` → robots check (Code) → HTTP Request with a polite Wait, or a fixture read → HTML node with the per-source selector → Crypto sha256 → skip if unchanged.
  - When changed: LLM gateway sub-workflow (strict JSON schema, per-field confidence, criteria classified `compilable|fuzzy|unknown_field` with `depends_on`) → validate (Code: ranges, annual→monthly, % parsing, tiers → most permissive) → diff → `product_criteria_history` + `needs_review` → upsert → deactivate products unseen for N days → `crawl_runs`.
  - Each source has its own error branch. The gateway paces calls to RPM and retries 429s with backoff (Wait + loop).
  - **Check:** products from ≥2 sources, and a rerun makes 0 LLM calls.
- **2b. Ingestion.** `services/{presign,ingest,notify,status}` plus `services/common/`.
  - `ingest` streams through `csv.reader` into `COPY staging`, then one SQL statement validates every row, upserts and closes the batch, in one transaction (validation moved from a typed Python model to SQL: DECISIONS AD32).
  - Rejects go to `rejects/{batch_id}.csv` via a SpooledTemporaryFile. It's idempotent on `(bucket, key, etag)`; for a repeated id, the last row wins and earlier rows are rejected.
  - `notify` signs the webhook as an HS256 JWT (AD11), reading the URL and secret from SSM. presign and status sign with a stdlib SigV4 instead of importing boto3 (AD33).
  - `gen_users.py --n --seed --dirty-rate --profile {real,wide}` and `make ingest-local FILE=`.
  - **Check:** pytest green against local Postgres (moto for S3 and SSM), and the 100k-row timing printed.
- **2c. Workflow B with S2.** `sql/match/*.sql` and `n8n/code/*.js` (pure functions with a thin `$input` wrapper). `scripts/build_workflows.py` injects them into `n8n/src` templates to produce `n8n/workflows`.
  - Flow: Webhook with native JWT Auth (HS256 = HMAC-SHA256, `exp`/`iat`; DECISIONS AD11) → Respond 202 → read the verified `batch_id` claim → idempotency via a conditional status UPDATE → S2 `INSERT … SELECT` (NULL thresholds never filter; fuzzy or low-confidence products give `pending_llm`) → `pipeline_metrics` → status → call C. Plus the sweeper trigger.
  - Reference evaluator: Python brute force over every pair.
  - **Check:** recall 1.0 against the reference.
- **2d. Workflow C.**
  - **First (ask the owner):** create the SES-only IAM user for n8n. Its policy allows only `ses:SendEmail` and `ses:SendRawEmail` on the verified identities in `AWS_REGION`. Its key goes into `SES_AWS_*` in `.env`. The owner's admin keys **never** go into an n8n credential, and bootstrap refuses if they match.
  - Flow: one `json_agg` query limited to `EMAIL_CAP` → render HTML and text (Code; LLM-decided matches labelled "may qualify"; caveats shown) → rewrite and assert the recipient → SES node, throttled → `notifications` + `notified_at` → one UPDATE marks the rest `suppressed`. It refuses to run unless `EMAIL_MODE=test_aliases`.
  - **Check:** a suppressed-mode run sends nothing. Then, **after asking**, one live email reaches `shivaset2+1`, and a rerun sends nothing.
- **Slice check:** `make demo-local` runs CSV → ingest → signed webhook → B → C.

### 3. AWS deploy (ask first). Unblocked: the account works in ap-southeast-2 (DECISIONS.md B1, AD27)
**Order until the account is unblocked or replaced:** 0 → 1 → 2 (local slice; Workflow C in suppressed mode, live email deferred) → 4 → 5. `serverless.yml` is still written and checked offline. This phase and the live email run as soon as a working AWS account is available.
- `infra/serverless.yml` covers:
  - the VPC (§3.9) and the bucket with CORS and lifecycle rules;
  - four functions (arm64, python3.14, 7-day log retention, a deps layer built with uv), destinations and the DLQ;
  - SSM params, RDS (`db.t4g.micro`, single-AZ, 20 GB gp3, no autoscaling, no Performance Insights, 1-day backups, `rds.force_ssl`, publicly accessible with security groups for my /32 and the Lambda SG, IAM auth on) and least-privilege IAM.
- Also (the SES-only IAM user already exists from 2d): migrations on RDS, the tunnel URL in SSM, and `make ui` (serves `ui/` with a `config.js` generated from stack outputs).
- **`make use-rds` / `make use-local`** (see the environment switch above) and **`make rds-stop` / `make rds-start`** so RDS doesn't run between sessions. Storage is still billed, and AWS automatically restarts a stopped instance after 7 days.
- `make destroy` plus a teardown checklist: empty the bucket, delete RDS and any final snapshot, delete the SSM params, delete the SES user's keys.
- **Check:** upload in the UI → status moves through uploaded → ingested → matched → notified, and one email arrives.
- **Status 2026-10-17:** written and compiled offline (`make sls-package`; DECISIONS AD35). RDS is pinned to 17.11, not the new default 18.3 (V16). The deploy, `make aws-params` and the live email wait for the owner.
- **Status 2026-10-18:** deployed (owner's go-ahead) and checked end to end. `data/users.csv` was uploaded through the page in AWS mode: S3, ingest with an IAM token, notify, the tunnel, then B and C on RDS, then one real email to `shivaset2+1` (SES message `010801a14d360397-…`). A replay sent nothing. RDS was stopped afterwards (DECISIONS V42, V43, AD37).

### 4. Optimisation, all additive behind flags
- Bench: `make bench` writes `bench/results/<variant>-<n>.json` and prints the table with these columns:
  - **pairs needing an LLM vs naive (headline)**, pairs left after each stage;
  - LLM calls, tokens, estimated cost at paid list price;
  - wall time per stage, recall and precision, LLM agreement with the oracle;
  - max items in any n8n node (read from `n8n-db` execution data), cache hit rate on a rerun.
  - Real data first, the `wide` profile second.
- S3 cohort signatures (bands relative to each product's own thresholds), S4 the `llm_decisions` cache, S5 K cohorts per call with one `UPDATE … FROM` fan-out. The S0 baseline is computed analytically and measured on a 100-pair sample.
- Oracle: about 200 stratified pairs judged with K=1 and cached. Then E2 (granularity), E3 (K ∈ {1,10,25,50}), E4 (whether the LLM changes ≥1% of decisions, which decides whether S5 stays on by default), and the ablation table.
- **Golden set:** draft `bench/golden/products.json` from the fixtures, then **stop for owner review**. Only then run E1: Flash vs Flash-Lite, cleaned text vs script-stripped HTML, one pass vs two that must agree, with an optional CSS/regex/hybrid comparison in worktrees.
- **Check:** every acceptance criterion at 1k, 10k and 100k:
  - recall 1.0;
  - match-time LLM calls stay flat as users grow;
  - a rerun makes 0 calls, and unchanged pages make 0 crawl calls;
  - no node holds more items than there are cohorts or emails.

### 5. Docs
- README:
  - a Mermaid diagram plus PNG;
  - setup including every n8n credential, the exact SES identities, and the v4/osls note;
  - the design decisions and the optimisation stance;
  - the ablation table and the experiment results;
  - the considered-and-rejected alternatives and cost notes.
- `docs/EXPERIMENTS.md`, and a demo recording plan kept out of git (`build/demo-script.md`).
- The private GitHub repo and collaborator invites, which I'll ask about first.

- **Status 2026-10-18 (review):** written: the README (diagram, setup, credentials, the optimisation, design decisions), `docs/architecture.{mmd,png}`, and a local demo plan in `build/demo-script.md` (not in git). `make demo-reset` sets up each workflow's full path (DECISIONS AD39). The demo video and its five source clips are linked at the top of the README; the repo invites are left for the owner.

## 6. What gets cut, in order, if time runs short
1. The E1 extras: the CSS/regex/hybrid strategy, separate worktrees, the two-pass variant.
2. E2 down to 2 granularities, and E3 down to K ∈ {1, 25}.
3. The Groq/OpenRouter branches. The gateway abstraction stays.
4. Running 100k through n8n end to end; 100k stays on the psql+node path.
5. The Hero FinCorp backup and the sweeper trigger.

**Never cut:** the thin slice, the upload UI, S2–S5, recall 1.0, the safety guards, the AWS deploy and teardown, the README with diagram, the demo script.

## 7. Free-tier LLM budget (estimate)
E1 about 150 calls, the oracle about 200, E2/E3 about 60, match time about 10 per new product set, crawl ≤3 per day (0 when pages are unchanged). That's about 450 calls over 2–3 days, inside 500 RPD. The naive S0 baseline would be about 250k calls for 10k users × about 25 products.

## 8. Verification
`docker compose up -d && make bootstrap && make test && make bench && make demo-local` locally. AWS and live email steps run only after asking.
