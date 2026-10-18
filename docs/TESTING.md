# Manual test guide

Each phase adds a section with the exact commands that check it. Run them from the repo root,
in order. Each section is cumulative: it assumes the earlier sections passed.

## Prerequisites

| Tool | Checked with | Version used to build |
|---|---|---|
| uv | `uv --version` | 0.12.23 |
| Python | `uv run python --version` | 3.14.6 (uv installs it if it's missing) |
| Docker and Compose | `docker compose version` | 27.5 / 2.32 (needed from phase 1) |
| GNU Make | `make --version` | any |

## Phase 0: skeleton and data profile

```bash
# 1. Install the pinned dev tools (ruff, mypy, pytest) from uv.lock into .venv
make setup

# 2. Lint, format check, strict type check: expect "All checks passed!" and "Success: no issues found"
make lint

# 3. Unit tests: expect all to pass (17 at phase 0; later phases add more)
make test

# 4. The committed profile matches a fresh run: expect "docs/PROFILE.md is up to date"
make profile-check

# 5. Or run 2 to 4 in one go
make check
```

What to look for in `docs/PROFILE.md` (it should match `docs/PLAN.md` section 2):

- 10,000 data rows, CRLF line endings, no BOM, **0 dirty rows**
- `email`: **2,632 distinct**, with up to **12 users** sharing one
- `monthly_income` 25,000–150,000 in steps of 1,000; `credit_score` 650–800 in steps of 5; `age` 22–50
- `employment_status`: Salaried 3,389 / Self-Employed 3,371 / Business 3,240
- Cohort cardinality: 3 for `employment_status` alone, 9,869 for the full profile

Check that no secrets are tracked: `git status --short` should list `.env.example` but never
`.env`.

## Phase 1: local stack

Needs Docker running. The first `make bootstrap` pulls about 3 GB of images.

```bash
# 1. Create .env from .env.example and generate the local secrets.
#    It prints key names only, never values, and never overwrites a value that's already set.
make env

# 2. One command: start containers, migrate app-db, seed app_config, import credentials and
#    workflows, publish them, restart n8n. Safe to re-run.
make bootstrap
#    expect: applied 001_roles.sql / 002_schema.sql / 003_grants.sql (or "database is up to date"),
#            "app_config: 23 keys stored (email_mode=suppressed)",
#            "Successfully imported 4 credentials", "Successfully imported 4 workflows",
#            and 4 "Publishing workflow" lines

# 3. Tests, now including the database tests: expect 53 passed
make check

# 4. The repo and n8n agree: expect "no drift: n8n matches n8n/workflows"
make export-workflows

# 5. Signed webhook through webhook-gw into Workflow B
make webhook-test                                  # expect: HTTP 202 {"message":"Workflow was started"}
make webhook-test ARGS="--mode unsigned"           # expect: HTTP 401 No token provided
make webhook-test ARGS="--mode bad-signature"      # expect: HTTP 403 invalid signature
make webhook-test ARGS="--mode expired"            # expect: HTTP 403 jwt expired

# 6. The gateway exposes nothing else: expect 404 404 404 403
for p in / /rest/settings /webhook-test/loan-match; do curl -s -o /dev/null -w "%{http_code} " "http://127.0.0.1:8088$p"; done
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/webhook/loan-match   # GET is refused

# 7. Optional: the public quick tunnel (exposes only webhook-gw). Stop it afterwards.
make tunnel                                        # prints https://<random>.trycloudflare.com
make webhook-test ARGS="--url $(make -s tunnel-url)"   # expect: HTTP 202
curl -s -o /dev/null -w "%{http_code}\n" "$(make -s tunnel-url)/rest/settings"   # expect: 404
make tunnel-down
```

In the UI at <http://localhost:5678>, create the owner account on the first visit, then check:

- **Overview → Workflows:** four workflows, all **Published**: *A · Loan product discovery*,
  *B · User-loan matching*, *C · User notification*, *LLM gateway (sub-workflow)*.
- **Overview → Credentials:** *App DB (n8n_app)*, *Gemini API key*, *Webhook JWT (HS256)*,
  *AWS SES (send-only IAM user)*. The SES one is empty on purpose until the SES-only IAM user
  exists (DECISIONS.md B1).
- **Executions:** one *B · User-loan matching* run per valid `make webhook-test`, each a success.
- **Open workflow A → Execute workflow:** *Load app_config* outputs one item whose `config`
  holds 23 keys, read through the least-privilege `n8n_app` role.

Optional security spot checks:

```bash
# Credentials are encrypted at rest: expect 0
docker compose exec -T n8n-db pg_dump -U n8n -d n8n | grep -c "$(grep '^WEBHOOK_HMAC_SECRET=' .env | cut -d= -f2)"
# Every host port is bound to 127.0.0.1 only
docker compose ps --format '{{.Service}}  {{.Ports}}'
```

## Phase 2a: Workflow A (product discovery) and the LLM gateway

Needs `GEMINI_API_KEY` in `.env`. A full re-extraction costs 3 Gemini requests; an unchanged
rerun costs 0. Free-tier Gemini sometimes answers 503 ("high demand"): the gateway retries with
backoff, and if all attempts fail that source is marked `error` and the others still run. Just
run the command again a few minutes later.

```bash
# 1. Pick up phase 2a: migration 004, the 3 crawl sources, the fixtures container, new workflows
make bootstrap                 # expect "applied 004_llm_calls_and_crawl_running.sql" (first time)
                               #        and "crawl_sources: 4 seeded (3 enabled)"

# 2. Tests: Python (incl. crawl SQL run as n8n_app) and the Code-node JavaScript
make check                     # expect 59 passed, and "ℹ pass 25" for the JS tests

# 3. First crawl from the saved pages: one LLM call per source
make crawl ARGS="--reset"
#    expect all three sources "changed", about 27 products from bankbazaar, 1 from hdfc-bank,
#    1 from kotak, and 3 llm calls (more if Gemini returned 503 and the gateway retried)

# 4. Run it again: nothing changed on the pages, so no LLM calls at all
make crawl
#    expect all three sources "unchanged" with llm = 0, and the product counts unchanged

# 5. The repo and n8n still agree
make export-workflows          # expect "no drift: n8n matches n8n/workflows"
```

Inspect the results:

```bash
# Products and their compiled hard filters (NULL means not stated: never filters anyone out)
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT lender, product_name, interest_rate_min, min_monthly_income, min_credit_score, min_age, max_age, allowed_employment FROM loan_products WHERE is_active ORDER BY lender;"

# How every leftover criterion was classified
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT s->>'class' AS class, count(*) FROM loan_products p, jsonb_array_elements(p.soft_criteria) s WHERE p.is_active GROUP BY 1;"

# Kotak's income tiers: the lowest is the filter, the rest are caveats
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT min_monthly_income, jsonb_pretty(soft_criteria) FROM loan_products p JOIN crawl_sources s USING (source_id) WHERE s.slug = 'kotak';"

# Criteria diffs between crawls (needs_review is set on changed products)
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT p.product_name, h.changed_fields, h.crawled_at FROM product_criteria_history h JOIN loan_products p USING (product_id) WHERE h.old_criteria IS NOT NULL ORDER BY h.history_id;"

# Every LLM request, including retries
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT created_at, purpose, model, attempt, http_status, outcome, tokens_in, tokens_out FROM llm_calls ORDER BY call_id;"

# Per-node view of the latest Workflow A execution (runs, item counts, errors)
PYTHONPATH=.:services uv run python scripts/n8n_execution.py --workflow lnWfProductDisc1
```

In the UI: open **A · Loan product discovery** → **Executions** to see each run. The sticky notes
on the canvas explain the stages and the failure paths. **Execute workflow** from the editor runs
the same flow through the external task runners.

## Phase 4: Workflow B (matching) and the optimisation

What this phase delivers and why: `docs/EXPERIMENTS.md` (measurements) and `docs/DECISIONS.md`
AD17–AD24. Everything below runs locally. Gemini is only called when a cohort or criterion is
new: on this machine every experiment answer is cached in `bench/llm_cache/`, so steps 5–6 cost
0 requests (on a fresh clone they would cost about 320).

```bash
# 1. Pick up phase 4: migrations 005-008, the MATCH_RULES_ENABLED switch, the new workflows
#    (incl. the "Warm match cache" sub-workflow) and the warm Code-node runner
make env                       # first time: "added MATCH_RULES_ENABLED"
make bootstrap                 # first time: "applied 005_matching.sql" ... "008_cohort_space.sql";
                               # publishes 5 workflows, and now waits until n8n serves the newly
                               # published versions (V28)

# 2. Tests: matching SQL run as n8n_app (incl. the vote of the two judges, replays, integrity
#    triggers), the Code-node JavaScript, lint and types
make check                     # expect "88 passed" and "ℹ pass 36"

# 3. Workflow B end to end through n8n, checked against a brute-force evaluator of every pair
make match-e2e ARGS="--n 10000"
#    expect: webhook HTTP 202; "matched after" about 2-3 s; recall 1.0000, precision 1.0000;
#    "at most 1 item(s) in one node". The first run after a bootstrap adds about 2 s (the runner
#    starts once); a batch with a new cohort or criterion adds one or two Gemini calls (7-15 s,
#    more when the free tier answers 503 and the gateway backs off).
make match-e2e ARGS="--csv data/users.csv"
#    the real CSV again: these users already have matches, so this exercises the upsert path
#    for returning users; same expectations.

# 4. The bench: the funnel, timings and S2 variants, every run checked against brute force.
#    No LLM calls (they are counted, and simulated as "uncertain").
make bench ARGS="--sizes 1000,10000 --products real"   # about 1 minute
make bench                                             # full matrix incl. 100k, about 15 minutes
#    expect "recall 1.000" on every row, "ok" for every S2 variant, 2 LLM calls per batch
#    whatever the size (one cohort call, one compile call; counted, not made), 0 on the rerun,
#    "warm" 2 and "after warm" 0: warming makes the same 2 calls at crawl time and leaves the
#    batch nothing to ask

# 5. The LLM-stage experiments: 14 strategies against the per-pair oracle (from the cache)
make bench-llm
#    expect "oracle self-consistency: ... decision_agreement: 1.0" and the last row
#    "threshold_with_rule_veto  2 ... 0.963 0.706"

# 6. The chosen strategy on the held-out sample (from the cache)
make bench-llm ARGS=--holdout
#    expect "drop_precision": 1.0 and "drop_recall": 0.92

# 7. Workflow A: unchanged pages still cost nothing, and the crawl now ends with the warm step
make crawl                     # expect all sources "unchanged" with llm = 0
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT run_id, pairs_in AS cohorts, pairs_out AS unjudged, duration_ms, details FROM pipeline_metrics WHERE stage = 'warm_cache' ORDER BY metric_id DESC LIMIT 1;"
#    expect cohorts 1, unjudged 0 (the real products' one cohort is cached), a few ms
make export-workflows          # expect "no drift: n8n matches n8n/workflows"

# 8. Warming doing real work (2 Gemini requests): a scratch DB with the synthetic products and
#    nothing cached, warmed through n8n, then a 10k batch through Workflow B
make warm-demo
#    expect "warm_cache cohorts 19 uncached/judged 19", "s5_llm ... llm calls 2", then the batch:
#    recall 1.0000, no s5_llm row for it, "LLM stages run at match time: 0", and finally
#    "n8n points at loans again"
```

Inspect the results:

```bash
# The latest batch: matches by status, and the stage funnel Workflow B recorded
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT b.batch_id, b.status, m.status AS match_status, count(*) FROM ingestion_batches b JOIN matches m USING (batch_id) WHERE b.batch_id = (SELECT batch_id FROM ingestion_batches ORDER BY received_at DESC LIMIT 1) GROUP BY 1, 2, 3;"
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT stage, pairs_in, pairs_out, llm_calls, tokens_in, tokens_out, duration_ms, details FROM pipeline_metrics WHERE batch_id = (SELECT batch_id FROM ingestion_batches ORDER BY received_at DESC LIMIT 1) ORDER BY metric_id;"

# Warm step runs (one per crawl; batch_id is empty because no batch is involved)
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT created_at, stage, pairs_in, pairs_out, llm_calls, details FROM pipeline_metrics WHERE batch_id IS NULL ORDER BY metric_id DESC LIMIT 6;"

# The two judges' caches: cohort verdicts and compiled rules (one row per criterion)
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT user_signature, verdict, reason, prompt_version FROM llm_decisions;"
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT criterion_text, jsonb_pretty(clauses), note FROM criterion_rules;"

# A few decided rows with their reasons and caveats
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT status, decided_by, reason, caveats FROM matches WHERE status <> 'matched' LIMIT 5;"

# Per-node timings and item counts of the latest Workflow B webhook run
PYTHONPATH=.:services uv run python scripts/n8n_execution.py --workflow lnWfUserMatchin1
```

Switches (in `.env`, then `make config` or `make bootstrap`):
- `COHORT_BANDS=threshold` (default; `coarse`/`fine` exist for the experiments),
  `LLM_BATCH_K=25`.
- `MATCH_RULES_ENABLED=false` turns the rule judge off: cohort verdicts alone decide.
- `MATCH_LLM_ENABLED=false` turns the match-time LLM off: fuzzy criteria become "Not checked"
  caveats and every pair that passes the hard rules is `matched`.

In the UI: open **B · User-loan matching**. The two sticky notes explain the stages and the call
formula; **Executions** shows each webhook and sweeper run, and the LLM gateway's runs appear
under **LLM gateway (sub-workflow)**. **Warm match cache (sub-workflow)** shows each warm step;
it can also be run from its editor ("Run manually").

To run Workflow B from its canvas (DECISIONS V34): B has a **Run manually** trigger that matches
every batch waiting in `ingested`, the state ingestion leaves a batch in.

```bash
make load-batch                # 1,000 new users as an 'ingested' batch, no webhook
#    expect "loaded 1,000 users as batch ..." and "status 'ingested'"
```

Then in n8n, reload **B · User-loan matching**. If the button doesn't already read
**Execute workflow from Run manually**, pick **Run manually** from the chevron next to it, and
click. Expect the canvas to fill in: Find waiting batches → Loop over batches → Claim batch →
Write matches → Notify users (Workflow C), once per waiting batch, then **Next batch** back into
the loop. With nothing waiting, the run ends at **Nothing waiting**.

The same path from the command line (it starts at the first trigger listed, Run manually):

```bash
make load-batch && make load-batch     # two waiting batches
docker compose run --rm --no-deps -T -e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_RUNNERS_MODE=internal -e NODE_FUNCTION_ALLOW_BUILTIN=crypto n8n execute --id=lnWfUserMatchin1
PYTHONPATH=.:services uv run python scripts/n8n_execution.py --workflow lnWfUserMatchin1
#    expect "Loop over batches run 0, 1, 2", "Write matches run 0, 1", "Next batch run 0, 1":
#    both batches matched and notified in one run
```

The webhook path on the canvas, if wanted: choose **Batch ingested (signed webhook)** in the same
chevron and click. n8n then listens on the test URL (one call, 2 minutes) and shows "Waiting for
you to call the Test URL". Within 2 minutes run `make match-e2e ARGS="--n 1000 --editor"`
(expect "webhook: HTTP 202 (editor test URL)"; without the click, "HTTP 404" and what to do).

## Phase 2d: Workflow C (notifications), suppressed mode

What this delivers and why: `docs/DECISIONS.md` AD5 and AD26. Everything below runs locally
with `EMAIL_MODE=suppressed`: nothing is sent, and the first `EMAIL_CAP` emails are rendered as
previews. A live email waits for the SES-only IAM user (the owner creates it; see the last step).

```bash
# 1. Tests: Workflow C's SQL as n8n_app (who is recorded, the cap, the alias order, that no
#    CSV address reaches the workflow, reruns, unsafe settings), and its Code nodes
make check                     # expect "104 passed" and "ℹ pass 44"
make bootstrap                 # imports Workflow C; the SES credential takes AWS_REGION

# 2. A batch through B and C. C records every user with a match as suppressed and closes the
#    batch in the same statement
grep '^EMAIL_MODE=' .env       # expect EMAIL_MODE=suppressed
make match-e2e ARGS="--n 10000"
#    expect "batch status: notified after ..." and "notifications: suppressed 10,000", the stage
#    rows plan_notifications and notify, then recall 1.0000, precision 1.0000

# 3. The emails C would send, as files (never sent)
make email-preview             # expect "(preview only, not sent)" twice and
                               # "2 email(s) written to build/email-preview/"
open build/email-preview/email-1.html

# 4. Nobody in the upload is an address: every recipient is empty in suppressed mode
docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT status, email_mode, count(*), count(recipient) AS with_recipient FROM notifications WHERE batch_id = (SELECT batch_id FROM ingestion_batches ORDER BY received_at DESC LIMIT 1) GROUP BY 1, 2;"
#    expect one row: suppressed | suppressed | 10000 | 0
```

Optional, the guard in n8n (nothing can be sent: the SQL refuses before recording anything):

```bash
docker compose exec -T app-db psql -U loans_admin -d loans -c "UPDATE app_config SET value = 'live' WHERE key = 'email_mode';"
docker compose exec -T n8n n8n execute --id lnWfNotificatio1
#    expect an error containing "EMAIL_MODE must be \"suppressed\" or \"test_aliases\"" and
#    "refusing to run"
make config                    # restores email_mode from .env
```

Later, after asking: the owner creates the SES-only IAM user (SendEmail/SendRawEmail on the
three verified identities in `AWS_REGION`), puts its keys in `.env`, sets
`EMAIL_MODE=test_aliases`, runs `make bootstrap` and one batch. Expect one email each at
`shivaset2+1@gmail.com` and `shivaset2+2@gmail.com` (possibly in Spam), and a rerun sends none.

## Region and round trips (`make timing`)

What this delivers and why: `docs/DECISIONS.md` AD27 (and V32, V33). AWS stays in
ap-southeast-2. Workflow B now makes 2 database round trips per cached batch (was 5), C makes 1
when nothing is sent (was 3), the LLM gateway 2 per call (was 3), the warm step 1 (was 2).
`make timing` measures the round-trip time and separates in-region work from network time.

```bash
# 1. The region is one variable
grep '^AWS_REGION=' .env       # expect AWS_REGION=ap-southeast-2
docker compose exec -T app-db psql -U loans_admin -d loans -At -c "SELECT value FROM app_config WHERE key = 'aws_region';"
#    expect ap-southeast-2 (after make bootstrap or make config)

# 2. Tests and the merged statements in n8n
make check                     # expect "104 passed" and "ℹ pass 44"
make bootstrap
make crawl                     # expect all sources "unchanged", llm 0

# 3. Probes plus a timed 10k batch (refuses unless EMAIL_MODE is suppressed and the cache is warm)
make timing ARGS="--run 10000"
#    expect:
#    - "TCP handshake sts.ap-southeast-2..." (about 390 ms from Bengaluru on this network) and
#      sts.ap-south-1 (about 35 ms);
#    - "B · User-loan matching ... round trips 2 (Claim batch 1, Write matches 1)";
#    - "C · User notification ... round trips 1 (Plan notifications 1)";
#    - "Warm match cache ... round trips 1 (Find unjudged cohorts 1)" under Workflow A;
#    - the split "in-region work / n8n on this laptop / network now / cross-region", and
#      "LLM calls 0";
#    - "report written to bench/results/timing-local.json".

# 4. Before vs after: pass any Workflow B webhook execution from before this change
make timing ARGS="--compare 144"   # expect execution 144 with "round trips 5" and its C with 3

# 5. The tunnel hop (starts the quick tunnel, then stops it again)
make tunnel
make timing ARGS="--samples 10"    # expect a "POST <name>.trycloudflare.com" line, HTTP 401
docker compose --profile tunnel stop cloudflared

# 6. Later, after the phase 3 deploy and `make use-rds`: the same report measured against RDS
make timing ARGS="--target rds --run 10000"
```

## Review: gateway fix, JIT, database settings, per-model LLM planning (2026-10-17)

What this delivers and why: `docs/DECISIONS.md` V35-V37, AD28, AD29, O1; `docs/EXPERIMENTS.md`
section 11. Steps 5 and 6 make 2 free-tier Gemini requests each; everything else makes none.

```bash
# 1. Pick up the new switch and the database settings
make env                       # first time: "added LLM_CONCURRENCY"
make bootstrap                 # recreates app-db with the compose settings, ends with
                               # "==> warming the Code-node runner (a no-op Workflow B run)"
docker compose exec -T app-db psql -U loans_admin -d loans -At -c "SELECT name, setting FROM pg_settings WHERE name IN ('shared_buffers','wal_compression','max_wal_size','fsync')"
#    expect shared_buffers 131072 (1 GB), wal_compression lz4, max_wal_size 8192, fsync on

# 2. Tests, including the SQL comment rule, the multi-line error log and the K planner
make check                     # expect "129 passed" and "ℹ pass 52"

# 3. The gateway bug (V35): run the gateway with no request. Expect "Execution was successful"
#    and the error "no request to send: ...", with no new row in llm_calls
docker compose run --rm --no-deps -T -e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_RUNNERS_MODE=internal -e NODE_FUNCTION_ALLOW_BUILTIN=crypto n8n execute --id=lnWfLlmGateway01 | grep -m1 "no request to send"
#    In the editor: open "LLM gateway (sub-workflow)", Execute workflow; it ends at
#    "Return result" with the same message instead of failing in "Log LLM call".

# 4. A batch through B and C; C's plan statement no longer pays for JIT
make match-e2e ARGS="--n 10000"
#    expect recall 1.0000, precision 1.0000; plan_notifications about 100-250 ms;
#    "Notify users (Workflow C)" under 1 s

# 5. Parallel LLM requests in the warm step (2 Gemini requests)
make warm-demo
#    expect the two gateway lines (compile_criteria, judge_cohorts) with the same duration,
#    about 4-5 s, then a 10k batch with "LLM stages run at match time: 0"

# 6. Workflow B judging its own cohorts, nothing warmed (2 Gemini requests)
make warm-demo ARGS=--lazy
#    expect "s5_llm ... 2" calls, both gateway lines with one shared duration, recall 1.0000

# 7. The cached experiments are byte-identical: with no usable key, any uncached request fails
GEMINI_API_KEY=invalid make bench-llm               # expect exit 0, threshold_with_rule_veto 2 calls 0.963 0.706
GEMINI_API_KEY=invalid make bench-llm ARGS=--holdout # expect "false_drops": 0

# 8. The full bench (about 10 minutes): same funnel, recall 1.000 in all 12 runs
make bench

# 9. Timing, after a batch
make timing ARGS="--run 10000" # expect B round trips 2, C 1, LLM calls 0
```

O1 is fixed (AD30): `make crawl ARGS="--reset hdfc-bank"` updates the same HDFC product even when the
LLM names it differently. Check that there is one row with
`docker compose exec -T app-db psql -U loans_admin -d loans -c "SELECT product_id, product_name, is_active FROM loan_products p JOIN crawl_sources s USING (source_id) WHERE s.slug = 'hdfc-bank'"`.

## Phase 2b: ingestion (the four Lambdas, offline) and the upload API's local stand-in

What this delivers and why: `docs/DECISIONS.md` AD32-AD34 and V38. The presign, ingest, notify
and status handlers in `services/` run against moto (S3, SSM) and the local `app-db` as
`lambda_app`; nothing reaches AWS. `make ui` serves the same three calls the deployed page will
make, with the same code behind them. `EMAIL_MODE` stays `suppressed`, so nothing is sent.

```bash
# 1. Migration 013 and the tests: ingest against an independent Python validator, the four
#    handlers under moto, SigV4 equal to botocore's, users.batch_id integrity and its lock
make migrate                   # expect "applied 013_users_batch_check.sql" (nothing if applied)
make check                     # expect "152 passed" and "ℹ pass 52"

# 2. The sample file, from CSV to notifications
make demo-local
#    expect "10,000 rows in ... ms -> 10,000 stored, 0 rejected", "webhook: HTTP 202" and
#    "notified ... s after the webhook: ... pairs, 0 LLM calls; emails {'suppressed': 10000, ...}"
make ingest-local              # the same file again, as a redelivered S3 event:
                               # expect "already ingested: batch ... (same file contents; ...)"

# 3. 100k rows, 2% dirty. Pick an unused --seed: a file ingested before is a redelivery
make gen-users ARGS="--n 100000 --seed 21 --dirty-rate 0.02 --out build/users-100k.csv"
#    prints "dirty rows: N {...}" with a count per fault
make demo-local FILE=build/users-100k.csv
#    expect "100,000 rows in about 2,000 ms", rejected = N minus padded_value (padded values
#    are trimmed and kept), "webhook: HTTP 202", then notified with 0 LLM calls
f=$(ls -t build/uploads/rejects/*.csv | head -1); cut -d, -f2 "$f" | sort | uniq -c
#    expect one line per fault with gen-users' count; "user_id repeated: ..." for the duplicates

# 4. The upload page's three calls, by hand. Terminal 1:
make ui                        # expect "upload page at http://localhost:8090"
#    Terminal 2: presign, the multipart POST the page sends to S3, then status
make gen-users ARGS="--n 2000 --seed 22 --dirty-rate 0.05 --out build/users-2k.csv"
KEY=$(curl -s -X POST localhost:8090/api/presign -d '{"size": 200000}' | python3 -c 'import json, sys; print(json.load(sys.stdin)["fields"]["key"])')
curl -s -o /dev/null -w "%{http_code}\n" -F key="$KEY" -F Content-Type=text/csv -F file=@build/users-2k.csv localhost:8090/api/upload
#    expect 204
sleep 3; curl -s "localhost:8090/api/status?key=$KEY" | python3 -m json.tool
#    expect "status": "notified", "done": true, rows total/valid/rejected, matching pairs by
#    status, emails {"suppressed": ...} and the ms of each stage
```

The page itself (http://localhost:8090) uses these calls. It is a placeholder until the UI
phase rebuilds it.

## Phase 3: the AWS stack, written and compiled offline (the deploy waits for the owner)

What this delivers and why: `docs/DECISIONS.md` AD35, V39 and V40, and `infra/serverless.yml`'s
header. Steps 1-2 create nothing in AWS. `make sls-package` makes one read-only CloudFormation
lookup with your profile (V40).

```bash
# 1. The guards and the artifacts
make check                     # expect "164 passed" and "ℹ pass 52" (tests/test_infra.py: no
                               # NAT, endpoint, proxy, LB, EC2 or secret; RDS shape; handlers)
make lambda-build              # expect app.zip (96 KB) and deps.zip (6.1 MB); run it twice:
                               # the hashes printed don't change
unzip -l build/lambda/deps.zip | grep cpython-314-aarch64   # the extension Lambda will load

# 2. Compile with osls, as CloudFormation (needs MY_IP_CIDR: your public IP as a /32)
echo "MY_IP_CIDR=$(curl -s https://checkip.amazonaws.com)/32"   # put this line in .env
make sls-package               # expect "✔ Service packaged"
python3 -c "import json,collections; t=json.load(open('infra/.serverless/cloudformation-template-update-stack.json')); print(sorted(collections.Counter(r['Type'] for r in t['Resources'].values()).items()))"
#    expect no NatGateway, Instance, DBProxy, LoadBalancer or SecretsManager types; one
#    VPCEndpoint (the S3 gateway); one DBInstance; two Lambda::Url
```

After asking (this creates AWS resources; RDS bills by the hour while it runs):

```bash
make tunnel                    # the quick tunnel; the notify Lambda reads its URL from SSM
make aws-params                # expect three lines: rds-master-password, webhook-secret,
                               # tunnel-url (values are never printed)
make deploy                    # Serverless v4, about 10-15 minutes (RDS); then it writes
                               # build/stack-outputs.json and prints "set in .env: RDS_HOST=..."
#    put RDS_HOST in .env, then:
make migrate-rds               # expect "applied 001_roles.sql" ... "applied 013_..."
make use-rds                   # n8n's app credential and app_config now point at RDS
UI_TARGET=aws make ui          # open http://localhost:8090 and upload data/users.csv
#    expect the steps uploaded -> ingested -> matched -> notified, with the row counts
make use-local                 # back to the local app-db for development
make rds-stop                  # every session ends here; `make rds-state` shows "stopping"
```

After every tunnel restart, run `make tunnel-ssm` (the URL changes). Teardown, after asking:
`make destroy` empties the bucket, removes the stack (RDS deleted with no snapshot) and deletes
the SSM parameters. Then check the console for leftovers: no RDS snapshots, no
`loan-engine-*` log groups, and the SES user's keys deleted.

## The upload page (Next.js, `ui/`)

What this delivers and why: `docs/DECISIONS.md` AD36 and V41. It needs Node 24. Everything
runs locally against the stand-ins in `scripts/ui_server.py`; `EMAIL_MODE` stays `suppressed`.

```bash
# 1. Install, test the page's logic, build
make ui-install                # npm ci from ui/package-lock.json (next 16.4.2, react 19.3.1)
make check                     # expect "165 passed" and "ℹ pass 66" (52 Code-node + 14 UI)
npm --prefix ui run typecheck  # expect no output

# 2. Serve it with the local stand-ins (presign, S3 upload, status) and open it
make ui                        # builds into ui/out, then "upload page at http://localhost:8090"
#    In the browser:
#    - "Sample CSV" downloads 25 synthetic customers; drop it on the card
#    - expect Upload, Check every row, Match loans and Notify customers to tick in turn, each
#      with its time, then "All 25 customers qualify for a loan" (or "N of 25") and four numbers
#    - drop a CSV without an email column: expect "This file has no email column" at once;
#      nothing is uploaded
#    - narrow the window to phone width: the card stacks, nothing scrolls sideways
make gen-users ARGS="--n 100000 --seed 31 --dirty-rate 0.02 --out build/users-100k-ui.csv"
#    drop build/users-100k-ui.csv: the upload bar moves, the row count is an estimate near
#    1,00,000 (Indian grouping), and the result
#    shows the skipped rows in the first step; the whole run takes about 10-15 s locally

# 3. Development with hot reload
make ui-dev                    # http://localhost:3000; edits under ui/app reload in place
```

After the deploy, `make stack-outputs` and `UI_TARGET=aws make ui` run the same page against
the Function URLs and the bucket.

### Phase 3, as run on 2026-10-18 (deploy, the live email, and back to safe)

The order that worked, after the owner's go-ahead. Each step names what it should print.

```bash
echo "MY_IP_CIDR=$(curl -s https://checkip.amazonaws.com)/32"   # into .env
make tunnel && make aws-params                    # tunnel URL; three SSM parameters
make deploy                                       # "Service deployed" (~12 min); RDS_HOST printed
make migrate-rds && make catalog-to-rds           # 001-014 applied; then rows synced per table and
                                                  # "RDS now has N active products, M matchable".
                                                  # Safe to repeat after any local re-crawl: it upserts
                                                  # by primary key and never deletes on RDS
make rds-ca                                       # certs/rds-global-bundle.pem (n8n trusts RDS)
# for the one real email: EMAIL_MODE=test_aliases and EMAIL_CAP=1 in .env, then
make use-rds                                      # n8n on RDS, SES-only credential
UI_TARGET=aws make ui                             # upload data/users.csv at http://localhost:8090
#    expect the four steps to finish (about 40 s from India), then "1 email sent · 9,999 held
#    back in test mode". The email arrives at shivaset2+1@gmail.com (check Spam)
# back to safe: EMAIL_MODE=suppressed and EMAIL_CAP=2 in .env, then
PYTHONPATH=.:services uv run python scripts/seed_config.py --target rds
make use-local && make rds-stop                   # "loan-engine-dev: stopping"
```

If the laptop slept, the quick tunnel is gone even though cloudflared still prints its old URL:
`docker compose --profile tunnel restart cloudflared && make tunnel-ssm` before the next upload
(V43). Teardown, after asking: `make destroy`.

## Review and demo preparation (2026-10-18)

What changed: unreadable files are refused with a reason (AD38); Execute Once on A's tail and
the warm query (V46); `make demo-reset` (AD39); the upload page's funnel, top bar, sections
(AD40); the README and the architecture diagram.

```bash
# 1. Everything static: lint, strict types, Python and JavaScript tests, generated files
make check
#    expect "All checks passed!", "Success: no issues found", "185 passed", "pass 73",
#    "docs/PROFILE.md is up to date", "n8n/workflows is up to date (5 workflows)"

# 2. The new tests on their own
uv run pytest tests/test_demo_reset.py tests/test_workflow_shapes.py -q   # expect 12 passed
uv run pytest tests/test_ingest.py -k unreadable -q                       # expect 3 passed

# 3. Load the updated workflows into n8n, and check the repo and n8n agree
make bootstrap && make export-workflows
#    expect "no drift: n8n matches n8n/workflows"

# 4. What a reset would do, without changing anything
make demo-reset ARGS="--dry-run"
#    expect "A: re-extracts hdfc-bank ...", "cache: drop verdicts for 1 cohort(s) and rules for
#    1 criterion(s)", and a "C: email_mode is ..." line saying whether C will send
```

**Every workflow on its full path** (local app-db; this calls Gemini 5 times. If `EMAIL_MODE`
is `test_aliases`, step 6 emails the 2 aliases; set it to `suppressed` and `make config` first
to send nothing):

```bash
# 5. A: one source through the LLM, the others skipped, then the warm step judging
make demo-reset                       # answer y
#    in n8n: open "A · Loan product discovery", click Execute workflow. Expect "Page changed?"
#    true for hdfc-bank, false for bankbazaar and kotak; "Extract with LLM", "Validate products"
#    and "Upsert products" ran once; "Crawl summary" 3 rows; under Executions the
#    "Warm match cache" run goes Find unjudged cohorts (1 item) -> LLM needed? (true) ->
#    Build cohort requests (2) -> Judge cohorts -> Store verdicts
make crawl-report                     # hdfc-bank "changed" with llm 1; the others "unchanged"

# 6. B: every branch, the LLM one included, then C
make demo-reset ARGS="--cold-batch 1000"   # answer y; "1 cohort(s) for B to judge"
#    in n8n: open "B · User-loan matching", chevron on Execute workflow -> Run manually -> click
#    (within 10 minutes, or the sweeper takes it). Expect Claim batch -> Claimed? (true) ->
#    LLM needed? (true) -> Build cohort requests -> Judge cohorts (LLM gateway) -> Attach cohort
#    refs -> Collect verdicts -> Store verdicts -> Write matches -> Notify users (Workflow C)
#    -> Next batch; then under Executions the C run, ending at Done

# 7. Item counts per node of the latest runs
uv run python scripts/n8n_execution.py --workflow lnWfProductDisc1
#    expect "Deactivate stale products ... items [1]" and "Crawl summary ... items [3]"
uv run python scripts/n8n_execution.py --workflow lnWfMatchWarm001
#    expect "Find unjudged cohorts ... items [1]" (it was 9 before V46)
```

**The upload page:**

```bash
make ui                               # http://localhost:8090, badge "Local demo"
#    drop data/users.csv. After about 1-3 s expect four ticked steps, then the result with the
#    funnel "How 2,40,000 possible checks were narrowed down" (about 62k pass, about 1.8k need
#    judgement, 0 AI calls). Below: How it works (4 steps), Where the criteria come from, Questions.
#    At phone width nothing scrolls sideways.
#    drop data/warmup.csv: 1 customer, "0 of 1 customers meet ...", no email.
#    a file with a NUL byte or a broken gzip now ends as "This file couldn't be processed"
#    with the reason (needs `make deploy` for the AWS ingest Lambda)
UI_TARGET=aws make ui                 # after make stack-outputs: badge "Live on AWS · ap-southeast-2"
```

The demo run order on AWS is in README §5, "Seeing every branch of every workflow".

## Hedged database logins and keep-warm (AD41, 2026-10-18)

```bash
# 1. Tests: slow login waited for, stuck login hedged, fast failures retried, dead connection
#    replaced, warm-ups never raise; the keep-warm schedule and timeouts in serverless.yml
uv run pytest tests/test_lambdas.py tests/test_infra.py -q        # expect 29 passed

# 2. Deploy (ask the owner first). It ends with the warm-up:
make deploy
#    expect "warm ingest: ok ..." and "warm status: ok ..."

# 3. The whole AWS path without a browser; data/warmup.csv matches nobody, so no email
make aws-upload
#    expect "uploaded ... ingested ... notified" in about 7-13 s from India (it was 1 min 42 s
#    when a fresh container's login stalled)

# 4. The schedule fires every 5 minutes and nothing fails
aws events list-rule-names-by-target --region ap-southeast-2 \
  --target-arn "$(aws lambda get-function --region ap-southeast-2 --function-name loan-engine-dev-status --query Configuration.FunctionArn --output text)"
#    expect one loan-engine-dev-KeepWarm-... rule; in CloudWatch, AWS/Events Invocations > 0 and
#    FailedInvocations 0 for it. A slow login shows up in the Lambda logs as
#    "db: logged in by attempt N of M after X s"; nothing is logged when it is quick.
```
