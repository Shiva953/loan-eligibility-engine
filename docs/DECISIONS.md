# Design decisions

Each entry gives the decision, the reason for it, and what it costs. Facts about external tools
were checked against live sources on **2026-10-16**. If a version changes, re-check the fact
before relying on it. The considered-and-rejected list at the end will grow as the
experiments report results.

## Versions and external facts

| ID | Fact | Source | Consequence |
|---|---|---|---|
| V1 | n8n **2.43.6** is the current stable (GitHub "Latest"; Docker `stable` points to the same build). The `latest`/`next` tags are being replaced by `stable`/`beta`. | [n8n releases](https://github.com/n8n-io/n8n/releases), Docker Hub `n8nio/n8n` | Pin `n8nio/n8n:2.43.6`, never a moving tag. |
| V2 | 2.x runs Code nodes on task runners. External mode needs the separate **`n8nio/runners`** image, **at the same tag** as n8n. | [Task runners](https://docs.n8n.io/hosting/configuration/task-runners/) | `n8nio/runners:2.43.6` runs as a sidecar. |
| V3 | In external mode, **`NODE_FUNCTION_ALLOW_BUILTIN`** goes in the runners container's launcher config `/etc/n8n-task-runners.json` under `env-overrides`. Setting it on n8n main has no effect. | same | Checked in the image: 2.43.6's default config **already** allows `crypto` (and the `moment` package). We mount `docker/runners/n8n-task-runners.json` anyway, so the allowance is pinned in the repo and doesn't depend on an image default. It allows `crypto` only (no external packages), and only the JavaScript runner is started. |
| V4 | 2.x breaking changes that matter here: `N8N_BLOCK_ENV_ACCESS_IN_NODE` defaults to `true`; Publish replaces activation; `import:workflow` deactivates what it imports; CLI publish needs a restart to take effect; `--tunnel` and the Start node are removed; sub-workflows need an Execute Workflow Trigger and must be published. | [2.0 breaking changes](https://docs.n8n.io/2-0-breaking-changes), [CLI](https://docs.n8n.io/hosting/cli-commands/) | Bootstrap runs import, then publish, then restart. A cloudflared sidecar replaces `--tunnel`. |
| V5 | Lambda **`python3.14`** is the newest GA Python runtime (AL2023). `python3.15` is in preview and isn't covered by the SLA. Every runtime supports arm64. | [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) | `runtime: python3.14`, `architecture: arm64`. **Re-checked 2026-10-17:** the runtime includes boto3 (its version depends on the region), so ingest and notify use it and only psycopg goes in the layer. |
| V6 | **psycopg-binary 3.3.6** ships `cp314 manylinux_2_27/2_28 aarch64` wheels. Lambda AL2023 has glibc 2.34. | PyPI `psycopg-binary` | Build the deps layer with `uv pip install --python-platform aarch64-manylinux_2_28 --only-binary :all:`, no Docker needed. |
| V7 | **Serverless Framework v4** (4.44.0) is free under $2M revenue but **requires a login** (dashboard account or `SERVERLESS_ACCESS_KEY`). **osls** 4.4.0 is a v3 fork maintained by the Bref team; it needs no login and reads the same `serverless.yml`. | [Upgrading to v4](https://www.serverless.com/framework/docs/guides/upgrading-v4), [osls](https://github.com/oss-serverless/osls) | Deploy with v4 under the owner's free login, and keep `serverless.yml` free of v4-only syntax so osls works too. |
| V8 | Stable Gemini models: `gemini-3.5-flash-lite` and `gemini-3.8-flash`; the 2.0 models are shut down. Google's docs now send you to AI Studio for free-tier RPM/RPD, and third-party figures disagree. | [Gemini models](https://ai.google.dev/gemini-api/docs/models), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) | Default to `LLM_RPM=10` and `LLM_RPD=500` in `app_config` until the owner supplies the AI Studio numbers. Limits apply per project and reset at midnight Pacific. |
| V9 | cloudflared **2026.10.1** is current. Quick tunnels need no account, and the URL changes every time the tunnel restarts. | Docker Hub `cloudflare/cloudflared` | **Confirmed in phase 1:** the metrics server's `/quicktunnel` returns `{"hostname": ...}`. `make tunnel-url` reads it, falls back to the logs, and will write the URL to SSM once AWS is usable (B1). |
| V10 | `_dmarc.gmail.com` is `p=none`. | `dig TXT _dmarc.gmail.com` | Gmail delivers SES mail sent "From: shivaset2@gmail.com" but fails DMARC alignment, so it may land in Spam. The setup docs warn about this. |
| V11 | Indian bank sites have moved to `.bank.in` (e.g. `hdfc.bank.in`, `kotak.bank.in`, `sbi.bank.in`), and the old domains redirect. | live fetches | Crawl sources use the new domains directly, which saves a redirect per fetch. |

| V12 | `import:credentials` encrypts plain-object `data` on import (`cipher.encryptV2` in `cli/src/commands/import/credentials.ts`). | n8n 2.43.6 source; checked in phase 1 (stored rows start `U2FsdGVkX1`, and the webhook secret appears 0 times in a `pg_dump`) | Bootstrap streams plaintext JSON built from `.env` into a temp file inside the container that only lives for the import. |
| V13 | Importing and publishing work before the n8n owner account exists. | phase 1 run | `make bootstrap` needs no UI step. The owner account is created on first visit to the UI. |
| V14 | The Postgres node evaluates `{{ … }}` anywhere in query text, **even inside SQL comments**. | phase 1 failure (`ExpressionExtensionError: invalid syntax`) | `build_workflows.py` rejects any included `.sql` file that contains `{{`. |
| V15 | `n8n execute` inside the running container fails, because the task-broker port 5679 is already in use. | phase 1 | Run CLI executions in a one-off container: `docker compose run --rm --no-deps -e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_RUNNERS_MODE=internal n8n execute --id=…`. |
| V16 | RDS's default PostgreSQL major version can't be checked: the SCP denies `rds:Describe*` (B1). | phase 1 | Local `app-db` runs `postgres:17.11-alpine`, which RDS supports. **Re-checked 2026-10-17** (`describe-db-engine-versions`, ap-southeast-2): the default is now **18.3**, and 17.11 is available. RDS is pinned to **17.11**, the version every measurement and migration here ran on (AD35). |
| V17 | HTTP Request with **full response + text format** puts the body in `data`, not `body`. JSON responses use `body`. | phase 2a failure (empty pages reached the LLM) | `collect_pages.js` and `plan_fetches.js` read `data`. The gateway reads `body`. |
| V18 | The Postgres node binds a JS array as a Postgres array literal, so an **empty array arrives as `'{}'`**, which `::jsonb` reads as an object. | phase 2a failure (`cannot call jsonb_to_recordset on a non-array`) | jsonb parameters are always passed as `JSON.stringify(...)`. |
| V19 | The Code-node sandbox (task runner) has **no `URL` global**. | phase 2a failure | `plan_fetches.js` parses paths with a regex. |
| V20 | An uncaught Code-node exception **stops the whole workflow** unless that node's error output is wired. Error-output items carry the message at top-level `message`. | phase 2a | Every Code node inside Workflow A's loop has an error output leading to a failure path, so one bad source never stops the others. |
| V21 | Gemini `generateContent` with `generationConfig.responseJsonSchema` gives schema-valid JSON on `gemini-3.5-flash-lite`. Nullable fields use type arrays, and `enum` and `additionalProperties: false` are enforced. Usage comes in `usageMetadata.promptTokenCount` / `candidatesTokenCount`. | live call, phase 2a | The gateway uses this, not the newer Interactions API. |
| V22 | Free-tier Gemini returns **503 "model is experiencing high demand"** in bursts. 5 attempts with backoff failed once, and the same call succeeded minutes later on attempt 3. | phase 2a | Retries with exponential backoff (4/8/16/32 s). A failed extraction keeps the previous products active, and tomorrow's run retries. |
| V23 | The Postgres node (pg-promise) formats `$1…$n` into the query text on the client and sends it as **one simple-protocol message** (`db.multi(pgp.helpers.concat(queries))`), so a query may hold several statements and they share one implicit transaction. | n8n 2.43.6 source (`Postgres/v2/helpers/utils.js`) | `write_matches.sql` starts with `SET LOCAL work_mem`. Tests and the bench run workflow SQL through a psycopg `ClientCursor` (`scripts/n8n_sql.py`), which binds the same way. |
| V24 | **Retry On Fail also works when the error output is wired:** the engine retries while the node returns an error item (`checkFailure` in `workflow-execute.js`). `maxTries` is clamped to 2–5 and `waitBetweenTries` to 0–5000 ms. | n8n 2.43.6 source | Write matches retries once (AD20). |
| V25 | With a launcher, the JavaScript runner exits after `N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT` idle seconds and the next Code node waits for a cold start: **2.2 s** measured. `0` disables the idle timer (`task-runner.js`: `if (this.idleTimeout === 0) return`). | n8n runners 2.43.6 source; Workflow B timing | `N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT=0` (AD24). |
| V26 | Free-tier Gemini latency is a **fixed cost per request**, not per token: a one-question call (≈53 output tokens) took a median 8–10 s, a 25-question call (885 tokens) 7 s, and only K = 50 (1,902 tokens) grew to 22 s. Thinking tokens are small (`tokens_out` includes `thoughtsTokenCount`). | 280 bench calls, 2026-10-16 | What matters on the critical path is the number of sequential calls, not their size (docs/EXPERIMENTS.md). |
| V27 | PL/pgSQL plans an `IF` condition as one expression. A transition table has only its own table's columns, so `OR`-ing a test of each table into one condition fails for both. | test failure, phase 4 | Each table gets its own branch (migration 006). |
| V28 | After a restart, n8n keeps serving the **previously published** version of a workflow for a few seconds, until it updates `workflow_published_version` (seen: a webhook 14 s after the restart ran the old version; the table switched 2 s later). | phase 4 e2e run | `make bootstrap` waits until `publishedVersionId = activeVersionId` for every workflow before it reports ready. |
| V29 | `INSERT … ON CONFLICT DO UPDATE … WHERE` **locks every conflicting row before it tests the WHERE**, so skipped updates still dirty pages and write WAL. | phase 4 measurement (a no-op replay of 2.37M rows took 32 s) | Unchanged rows are filtered out by a read-only anti-join before the INSERT (10.4 s); the WHERE stays as a guard. |
| V30 | Gemini rejects a structured-output schema with two parallel arrays of the same nullable-field clause object (HTTP 400 "invalid argument"); the same clause in one array with a `verdict` enum is accepted. | phase 4 bench | The rule judge's schema uses one tagged clause list. |
| V31 | The AWS SES node (v1) signs its request for service `email`, but the credential's custom-endpoint override is only read for service `ses`, so **a custom SES endpoint is ignored** and the node always calls `email.<region>.amazonaws.com`. | n8n 2.43.6 source (`nodes/Aws/SES/AwsSes.node.js:800` calls `awsApiRequestSOAP('email', …)`; `credentials/common/aws/utils.js:477` reads `sesEndpoint` only `if (service === 'ses')`) | No local SES mock through n8n. Workflow C's send branch is covered by Code-node tests and SQL tests; one live email waits for the owner (2d). |
| V32 | The Postgres node keeps **one pg pool per credential** (`ConnectionPoolManager`, dropped after 5 idle minutes, swept every 60 s). Inside the pool, node-postgres closes a connection idle for **10 s** (`idleTimeoutMillis` default; the credential can't change it). | n8n 2.43.6 source (`nodes/Postgres/transport/index.js`: `keepAlive`, `max` only; `utils/connection-pool-manager.js`: `ttl = 5 * 60 * 1000`, 60 s sweep); `pg-pool@3.14.0` `idleTimeoutMillis = 10000` default | Statements of one run reuse one connection, but a webhook after 10 quiet seconds opens a new one: about 5 round trips (TCP, SSLRequest, TLS 1.3, two SCRAM exchanges), 1.9 s from Bengaluru to Sydney (AD27). |
| V33 | A SQL set-returning function called with a **column** instead of a literal is still inlined, but the planner can no longer fold the body for that value: `band_label()`'s mode test ran on every row (+25% for the cohort query at 100k users), and `cohort_space()`'s estimate grew to ~10⁹, so **JIT** compiled a 2 ms query for 0.5 s (5 s in a new n8n connection). | phase 4 measurement, `EXPLAIN (ANALYZE)` | The statements that read the banding mode from `app_config` have one branch per mode with the mode as a literal; a one-time filter runs only the configured branch. Both also `SET LOCAL jit = off` (at 100k users the cohort query went from 1.61 s to 1.36 s). |
| V34 | An Execute Workflow node returns the sub-workflow's result on the **same output index as the sub-workflow's last node**. A sub-workflow that ends on an IF's false branch (or a loop's or an error's second output) hands its data back on output 2, which the caller's single-output node has no connection for, so the caller's run **stops there without an error**. Clicking **Execute workflow** on a workflow that starts with a Webhook only listens on the test URL (one call, 2 minutes, mode `manual`). `n8n execute` starts at the first node in the list whose type can start a run (an Execute Workflow Trigger first). | n8n 2.43.6: Workflow B matched only the first of two waiting batches (executions 184-186); source `dist/utils.js` `findWorkflowStart`, `dist/webhooks/test-webhooks.js`, `TEST_WEBHOOK_TIMEOUT` = 2 min | Workflow C ends every path in one **Done** node; `tests/test_workflow_shapes.py` fails if any sub-workflow output after the first is unwired. B has a **Run manually** trigger, listed first, which matches every batch waiting in `ingested` (`make load-batch` adds one). |
| V35 | The Postgres node formats the query with pg-promise, which replaces `$1…$n` **everywhere in the text, comments included**. A comment that names a parameter receives its value; a value with a line break (Gemini's 400/429 errors are bullet lists) ends the comment and the rest is parsed as SQL: `syntax error at or near "*"`. | n8n 2.43.6, gateway execution 204 ("Log LLM call"), reproduced with psycopg | Workflow SQL names its parameters `#1`, `#2` in comments; `scripts/sql_text.py` finds comments the way Postgres does and `build_workflows.py` refuses a `$n` inside one; a test logs a multi-line error. The gateway also refuses to call the provider when run with no request. |
| V36 | **Execute Workflow in "each" mode runs the sub-workflows one after another** (`await` inside a `for` loop), so a loop of single-request gateway calls can't overlap. **The HTTP Request node sends all of its items concurrently** (`Promise.allSettled`); its `batching` option (read per item, so expressions work) sends `batchSize` at a time and sleeps `batchInterval` between groups. In a Code node, `$('Node').all(branch, run)` reads any run and branch, and a run that doesn't exist throws `Run N of node "…" not found`. A sub-workflow hands back only its last node's last run. | n8n 2.43.6 source: `ExecuteWorkflow.node.js:358-365`, `HttpRequestV3.node.js:195-199, 583`, n8n-workflow `workflow-data-proxy.js:411` | The gateway takes every request of a batch in one call and lets the HTTP node send them `llm_concurrency` at a time under `llm_rpm`; "Return result" reads every pass with `all(branch, run)` (AD29). |
| V37 | The planner's estimate for Workflow C's plan statement is about **1.5M**, above `jit_above_cost` (100,000) and the inlining and optimisation thresholds (500,000), so **JIT compiled it on every run for about 0.7 s** while it executes in about 0.25 s at 10k users. The write statement sits at about 95,000 at 10k users, just under the threshold. | `EXPLAIN (ANALYZE)` JIT timing; 10k batches, JIT on 1.13-1.66 s vs off 0.24-0.28 s; write A/B 1.61 vs 1.63 s (10k), 26.9 vs 26.8 s (100k); replay 13.8 vs 13.5 s | `SET LOCAL jit = off` in the plan, write, cohort and warm statements (V33). The plan statement went from 1.25 s to about 0.27 s; the others measured neutral, and turning JIT off removes the cliff at the threshold. |
| V38 | **ANALYZE's cost is in the per-column statistics, not the sample.** `ANALYZE users (batch_id)` reads the same 30k-row sample as `ANALYZE users`, but skips sorting the text columns: 18–27 ms against 77–180 ms at 160k users. The first ANALYZE after a large write is slower either way (128 ms, then 18 ms), because it is the first reader to set the new tuples' hint bits. `INSERT … ON CONFLICT DO UPDATE` fires both the INSERT and the UPDATE statement triggers, each with its own transition table. | measured, phase 2b (local Postgres 17.11) | AD34: analyze only `batch_id` after a small batch; one trigger per event. |
| V39 | **A function URL created since October 2025 needs two grants**: `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` (with `InvokedViaFunctionUrl: true`). The console and SAM add both; CloudFormation users add them. osls 4.4.0 compiles both from `url:`; Serverless v4's behaviour isn't documented. | [URL access control](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html), [AWS::Lambda::Permission](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lambda-permission.html), the compiled template | `serverless.yml` declares the `InvokeFunction` grant itself, so the URLs work whichever CLI compiles it (a duplicate grant is harmless). |
| V40 | **osls `package` makes one read-only AWS call** (CloudFormation `DescribeStacks`, to compare the layer with the last deploy), so it needs credentials but creates nothing. osls rejects a nested fallback such as `${opt:stage, ${env:STAGE, 'dev'}}`. RDS postgres17 defaults: `rds.force_ssl` 1, `jit` 0, `wal_compression` zstd, `rds.log_retention_period` 4320 (max 10080). An `ssm-secure` reference works for `AWS::RDS::DBInstance` `MasterUserPassword`. | osls 4.4.0 runs, `describe-engine-default-parameters`, [ssm-secure](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-ssm-secure-strings.html) | `make sls-package` uses the owner's profile. `--stage` comes from `STAGE` via `scripts/sls.py`. The master password stays in SSM. |
| V41 | **Next.js 16.4.1 (2026-10-14) is a security release**: two critical unauthenticated RCEs (`use cache` returning typed arrays, GHSA-673q-mjm6-r5xr; Image Optimization with SVG, GHSA-2464-fhr4-w9xw) and a Server Actions DoS (GHSA-p3m5-59mp-jv5r). React shipped 19.3.1/19.2.9/19.1.10/19.0.9 the same day ("Improve transport protocol" for Server Components). `latest` is 16.4.2. Node 24 runs `.ts` tests directly (type stripping), given imports with explicit `.ts` extensions. | [Next.js v16.4.1 release](https://github.com/vercel/next.js/releases/tag/v16.4.1), [React releases](https://github.com/facebook/react/releases), npm | Pinned **next 16.4.2, react 19.3.1** although they are under two weeks old: a security fix outranks the age rule. TypeScript 5.9.3, `@types/node` 24 (the local Node). The UI's logic tests run with `node --test`, no build. |
| V42 | **n8n's AWS SES node sends the whole message in the request URL** (`POST /?Action=SendEmail&Message.Body.Html.Data=...`, empty body), and the SES endpoint refuses long URLs: a 9 KB URL was accepted, an 18 KB one got `414 Request-URI Too Large` before authorisation. A 24-product email (11 KB of HTML, 25 KB encoded) failed with n8n's generic "Your request is invalid". | probed with the SES-only user to a recipient its policy denies (nothing deliverable), n8n 2.43.6 source | `render_emails.js` keeps the encoded HTML under 7,000 characters: best rates first, then "…and N more". The text version keeps every product. |
| V43 | Found on the first AWS deploy (2026-10-18). **EC2 accepts only `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*` in security-group descriptions**: an apostrophe failed the stack, and Serverless v4 then deleted the failed new stack by itself. **A quick tunnel dies when the laptop sleeps**: cloudflared keeps reporting its old URL, the hostname stops resolving, and the notify Lambda sees `[Errno 16] Device or resource busy`. **An in-VPC Lambda's first call seconds after a deploy timed out**, and the next ones answered in 1.2-1.9 s. AD10 had never been wired into compose, so n8n rejected RDS's certificate ("self-signed certificate in certificate chain"). | the deploy and the live run | `tests/test_infra.py` checks every SG description. After sleep: `make tunnel`, then `make tunnel-ssm`. n8n mounts `certs/` and sets `NODE_EXTRA_CA_CERTS` (`make rds-ca`). |
| V44 | **The first login from a new Lambda container to an RDS that has just started can exceed 5 s** (TCP, TLS and the IAM-token check all count against libpq's `connect_timeout`). The ingest of a 10k file failed its first attempt with `ConnectionTimeout`, and Lambda's async retry ran it about 60 s later in 1.6 s: the upload took 1 min 30 s instead of about 30 s. The status function did the same once after the deploy. | CloudWatch logs, 2026-10-18 06:35 and 06:52; the RDS log shows no trace of the stalled login, so it never reached Postgres | ~~`common/db.py`: attempts of 3, 5 and 10 s, retried at once~~. **Superseded by AD41:** the stall is RDS's IAM check, and cancelling a slow check restarts it, so short attempts could fail every time. Logins are now hedged and never cancelled, and the in-VPC Lambdas are kept logged in. |
| V45 | **From India each new HTTPS connection to Sydney costs about 1.2 s** before data moves (TCP 0.4 s, TLS 0.4 s, request 0.4 s, measured with curl), and browsers drop unused preconnections after about 10 s. A CSV gzips about 2.8x (970 KB to 349 KB). The notify role allowed `ssm:GetParameter` but the handler now calls `ssm:GetParameters`; moto doesn't enforce IAM, so only the live run caught it. | measured 2026-10-18 | The page gzips in the browser (CompressionStream; ingest decompresses by magic number), fetches the upload link on load and refreshes it every 4 minutes, and re-opens connections when a file is dragged in or the button is pressed. Notify has 512 MB (its cold start went from 5.8 s to 0.08 s). `tests/test_infra.py` ties the notify role to the call the code makes. **A 10k upload on AWS went from 99 s to 12.7 s** (upload 3.6 s, check 0.4 s, match 5.5 s, notify 0.6 s with email suppressed; two real sends add about 5 s). |
| V46 | **A Postgres node runs its query once per input item**, and a loop's "done" output carries one item per iteration. Workflow A's tail received 3 items (one per source): Deactivate stale products ran 3 times, Crawl summary 3 times (9 rows), and the Warm match cache sub-workflow, fed those 9 items, ran its query 9 times and wrote 9 `warm_cache` metric rows. The node setting **Execute Once** runs a node with the first item only. | execution 634, 2026-10-18 review | Execute Once on Deactivate stale products, Crawl summary and Find unjudged cohorts (the gateway's Load app_config already had it). `tests/test_workflow_shapes.py` pins all four. Re-run: 1 deactivate, 3 summary rows, 1 warm query. |
| V47 | **RDS IAM authentication runs through an auth proxy on the instance** (`rdsauthproxy`, `127.0.0.1:1108`, `POST /authenticateRequest`). Its curl traces land in the Postgres log, without timestamps. Timed phase by phase from Bengaluru, an IAM login to Sydney took TCP 0.3 s, TLS 1.0 s, and 2.6–5.3 s for the whole login, so the server's IAM check alone took 0.7–3.4 s, slowest on the first login after a quiet spell. Inside the VPC a round trip is about 1 ms, so a Lambda's login time is that check. | RDS log `error/postgresql.log.2026-10-18-11`; phase-timed probe, 2026-10-18 | AD41 |

## Blockers

### B1. AWS "blocked by an Organizations SCP" (found 2026-10-16). RESOLVED 2026-10-17: the account is pinned to ap-southeast-2
- **Finding (2026-10-16):** account `750330851091` is a member of organization `o-e7w4seapot` (management account `062534561957`). SCP `p-4klo6uwh` explicitly denied Lambda, RDS, S3, CloudFormation, SSM, SQS, EC2/VPC, SES and CloudWatch Logs in every region probed: ap-south-1, us-east-1/2, us-west-2, eu-west-1, eu-central-1, ap-southeast-1. IAM, STS, Budgets and Organizations reads were allowed.
- **Cause (owner, 2026-10-17):** the account was created through AWS's simplified sign-up, which pins a project to one region chosen by country. That region is ap-southeast-2, the one region the probes had not tried. Re-probed the same day: SES, Lambda, S3 and RDS all answer in ap-southeast-2, and the deny still applies elsewhere. Moving to another region needs the Paid plan plus advanced features that can't be undone, against the brief's no-cost expectation.
- **Decision (owner):** stay in ap-southeast-2, with the region as one variable (`AWS_REGION`). See AD27 for what this costs in latency and how production would avoid it.
- **State after the fix:** SES in ap-southeast-2 has sending enabled, is in the sandbox (200 emails a day, 1 per second), and has `shivaset2@gmail.com`, `shivaset2+1@gmail.com` and `shivaset2+2@gmail.com` verified (re-verified by the owner in Sydney). Phase 3 and the live email in 2d still wait for the owner's go-ahead (creating AWS resources and sending email are ask-first actions).

## Data (see `docs/PROFILE.md`)

| ID | Decision | Why |
|---|---|---|
| DA1 | `user_id` (UUIDv4) is the primary key. `users.email` has **no unique constraint**. Notifications are keyed on `user_id`. | 2,632 distinct emails across 10,000 users, with up to 12 users per email, each with different attributes. |
| DA2 | Keep the `name` column. | It isn't in the brief's column list, but it's in the file and makes the email greeting personal. |
| DA3 | Cohort signatures are built per criterion from its `depends_on` fields, never from the full profile. | 9,869 distinct full profiles among 10,000 users, against 3 signatures for an employment-only criterion. |
| DA4 | `gen_users.py` samples each field from its own distribution and adds a `--dirty-rate`. | The fields are independent (\|r\| < 0.05) and the sample has 0 dirty rows, so the reject path needs injected bad rows. |
| DA5 | The ingest reader is built for CRLF input with an optional BOM. | The sample uses CRLF, and spreadsheet exports often add a BOM. |

## Architecture changes (accepted 2026-10-16)

These sit on top of the kickoff architecture: presigned upload → S3 → `ingest` in the VPC → `notify` outside the VPC → n8n through cloudflared; a `status` Lambda; RDS reachable only from the owner's /32 and the Lambda security group.

### AD1. RDS IAM authentication for the in-VPC Lambdas
- **Decision:** `ingest` and `status` connect as the `lambda_app` role using an IAM auth token, not a password. n8n uses a separate least-privilege `n8n_app` role with a password, and migrations run as the master user. Locally, `DB_AUTH=password` uses the same code path with a password.
- **Why:** with no NAT Gateway and no interface endpoints, which the cost rules forbid, an in-VPC Lambda can't reach `ssm.<region>.amazonaws.com`, so it can't read a SecureString at runtime. `generate_db_auth_token` is an offline SigV4 presign, so it needs no network call. No password is stored anywhere on the Lambda side.
- **Cost:** you need `rds-db:connect` in IAM and `GRANT rds_iam`. Tokens last 15 minutes, which is fine for per-invocation connections. The fallback, resolving `${ssm:…}` into an environment variable at deploy time, is simpler but leaves the password in the function configuration.

### AD2. Lambda Destinations instead of an S3 "done marker"
- **Decision:** `ingest` gets an `onSuccess` destination of `notify` and an `onFailure` destination of an SQS DLQ. For a duplicate `(bucket, key, etag)`, `ingest` returns `{duplicate: true}` and `notify` does nothing. `make replay BATCH=…` re-sends a webhook by hand.
- **Why:** S3 invokes `ingest` asynchronously, so the Lambda service itself delivers the destination. That works from inside a VPC with no NAT. It removes the marker prefix and the second bucket notification, and `notify` receives the counts in `responsePayload`.
- **Cost:** no S3 object leaves an audit trail. `ingestion_batches` and the DLQ provide that instead.

### AD3. `webhook-gw` in front of n8n
- **Decision:** cloudflared forwards to an nginx:alpine container that passes only `POST /webhook/<WEBHOOK_PATH>` to n8n and returns 404 for everything else. The n8n UI listens on `127.0.0.1` only.
- **Why:** a quick tunnel pointed straight at n8n would put the editor and the REST API on the public internet.
- **Cost:** one more small container.

### AD4. An `app_config` table instead of `$env` in workflows
- **Decision:** bootstrap seeds non-secret switches from `.env` into `app_config` (email mode, cap and aliases, crawl mode, LLM provider, model and limits, cohort banding, batch size, whether the match-time LLM is on). Every workflow reads it in its first node. Secrets live only in n8n credentials.
- **Why:** this keeps n8n 2.x's hardened default `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`. With env access open, an expression could read `N8N_ENCRYPTION_KEY`.
- **Cost:** a changed `.env` needs `make bootstrap` (or `make config`) to take effect.

### AD5. A `notifications` table and suppressed mode
- **Decision:** each row of `notifications` records the user, batch, real recipient alias, SES message id, status (`sent | suppressed | failed`) and time.
  - In `test_aliases` mode, Workflow C sends to at most `EMAIL_CAP` users, in a deterministic order, mapped to the verified aliases.
  - The same SQL statement records every other user in the batch as `suppressed` (AD26).
  - Workflow C refuses to run unless `EMAIL_MODE` is `suppressed` or `test_aliases` and the cap is between 1 and the number of aliases (checked in SQL before anything is recorded, and again in a Code node).
- **Why:** it guarantees that a rerun sends nothing new, it audits where every email actually went, and no n8n node ever holds more items than there are emails.
- **Cost:** another table.

### AD6. Sweeper trigger on Workflow B
- **Decision:** a second trigger runs on a schedule (every `SWEEPER_STALE_MINUTES`). It picks up batches stuck in `ingested`, then follows the same idempotent path as the webhook.
- **Why:** if the tunnel was down or `notify` gave up, no batch should be left behind.
- **Cost:** one cheap query every 10 minutes.

### AD7. Isolate worktrees with Compose projects, not DB schemas
- **Decision:** each git worktree sets its own `COMPOSE_PROJECT_NAME` and host ports, so it gets its own `app-db`, `n8n` and volumes.
- **Why:** an n8n Postgres credential pins one database. Separate containers give isolation with no schema plumbing in the SQL.
- **Cost:** more RAM when several worktrees run at once.

### AD8. Cost and abuse guards
- **Decision:**
  - S3 lifecycle rules expire `uploads/` and `rejects/` after 7 days.
  - A presigned POST is limited to 50 MB, expires after 5 minutes, uses a key the server generates, and requires `text/csv`.
  - No reserved concurrency: new accounts may have too low a concurrency quota to reserve any.
  - RDS runs only while in use (`make rds-stop` / `make rds-start`).
- **Why:** the upload endpoint is unauthenticated (a public Function URL), so storage and request size have to be bounded.
- **Cost:** a publicly accessible RDS instance gets a **public IPv4 address, billed at about $0.005 per hour**, which comes out of credits. No free design avoids it, because the local n8n has to reach RDS. A stopped instance is still billed for storage, and AWS starts it again automatically after 7 days.

### AD9. A dedicated minimal VPC
- **Decision:** `serverless.yml` creates its own VPC: two public subnets, an internet gateway, a route table, the **S3 gateway endpoint** (free) and two security groups.
- **Why:** everything lives in one stack, so teardown is complete and the default VPC is never changed. Lambda ENIs don't get public IPs, so in-VPC Lambdas still have no internet access. That's expected: S3 goes through the gateway endpoint, and the Lambda service delivers destinations and logs.
- **Cost:** a little more CloudFormation.

### AD10. Verified TLS from n8n to RDS
- **Decision:** mount the AWS RDS CA bundle into the n8n container and set `NODE_EXTRA_CA_CERTS`. The Postgres credential uses SSL with verification on.
- **Why:** RDS's CA isn't in Node's default trust store, and the usual shortcut ("ignore SSL issues") encrypts the connection without authenticating the server.
- **Cost:** one extra file to download (`make rds-ca`).

### AD11. The webhook is signed as an HS256 JWT and verified by n8n's Webhook node
- **Decision:** `notify` sends `Authorization: Bearer <JWT>`. The token is HS256 (which is HMAC-SHA256) with `iat`, a 5-minute `exp`, a `jti`, and the batch facts (`batch_id`, counts) as claims. Workflow B's Webhook node uses its built-in **JWT Auth** against the `jwtAuth` credential, and the workflow trusts only the verified claims, never the request body. The path is a fixed constant, `/webhook/loan-match`, shared by Workflow B, nginx and `notify`.
- **Why:** the kickoff asked for an HMAC-SHA256 signature plus a timestamp, verified in a Code node. But Code nodes can't read credentials, and `$env` stays blocked (AD4). So the shared secret would have had to sit in workflow JSON or flow through execution data, which n8n stores. With native JWT verification, the secret stays in an encrypted credential. Signing uses the standard library only (`services/common/webhook_jwt.py`), and its signing primitive is pinned to the jwt.io reference vector in the tests.
- **Checked in phase 1, through webhook-gw and through the tunnel:** a valid token gets 202, a missing token 401, a bad signature 403, an expired token 403.
- **Cost:** a token replayed within its 5 minutes is accepted, but the batch-status idempotency check makes that a no-op.

### AD12. A fixture server, so fixture and live runs share one code path
- **Decision:** a `fixtures` container (nginx, internal network only) serves `tests/fixtures/crawl/<slug>/`. In `crawl_mode=fixtures`, Workflow A fetches `http://fixtures/<slug>/<file>` through the same HTTP Request nodes, robots check, cleaner and hash it uses for the live sites. The saved robots.txt is still checked against the *real* page URLs.
- **Why:** reading files from disk would be a second code path that tests and demos could pass while live crawling breaks. n8n 2.x also restricts file access by default.
- **How fixtures are made:** `make fixtures` (`scripts/fetch_fixtures.py`) follows the same etiquette as the workflow (robots.txt, our User-Agent, a 5 s pause) and writes a `manifest.json` with the URL, status, time and SHA-256 of each file.

### AD13. A structure-preserving cleaner, with the hash taken over the cleaned text
- **Decision:** a Code node (`clean_html.js`) turns each page into text that keeps table rows as `cell | cell` lines and headings as `##` lines. It drops scripts, nav, header, footer and forms, removes mega-menu link lists (runs of 4+ short bullets with no figure), and removes duplicate lines. The SHA-256 (Crypto node) is taken over this text.
- **Why:** none of the pages has a `<main>` element and their IDs are machine-generated, so CSS selectors in n8n's HTML node would break on the next redeploy. Plain text extraction would also detach a lender's name from its rate in the rate tables. Hashing cleaned text means cosmetic markup changes don't trigger an LLM call.
- **Result on the fixtures:** BankBazaar's 711 KB of HTML becomes 50 KB of text, HDFC's 1.1 MB becomes 4 KB, and every rate-table row survives (pinned by a test).

### AD14. The LLM reports what the page says; code decides what filters
- **Decision:** the extraction schema asks for values *as stated*: income amounts with their period and any condition, a credit score with its kind (`minimum` / `preferred` / `not_stated`), a confidence per field group, and every other criterion classified `compilable` / `fuzzy` / `unknown_field` with `depends_on`. `validate_products.js` then:
  - converts annual income to monthly;
  - uses the most permissive income tier as the SQL filter, turning stricter employment-specific tiers into `compilable` rules and condition-specific tiers into `unknown_field` caveats;
  - turns a "preferred" score into a ranking rule, not a filter;
  - checks ranges and puts swapped values back in order;
  - applies S6: a hard-filter field below `extraction_confidence_min` is stored as NULL and becomes a `fuzzy` criterion carrying the stated value.
  `criteria_hash` covers decision-relevant criteria only (no confidences, no evidence).
- **Why:** the LLM never does arithmetic or threshold logic the engine depends on, and all of that logic is unit-tested (25 JS tests).
- **Prompt v1 → v2 (a finding E1 will measure):** v1 extracted 16 BankBazaar products and skipped lenders that appear *only* in the rate or eligibility tables (SBI, Axis, PNB, Union Bank, UCO, ...). v2 states that every table row is a product, and that contradictory values on the same page lower the confidence. That gave 27 products. Note that BankBazaar itself contradicts itself: its HDFC card says 10.85% while its table says 10.50%.
- **Observation for E4:** the real pages produce almost no `fuzzy` criteria (22 `unknown_field`, 1 `compilable`, 0 `fuzzy` across 30 products). Most residual criteria depend on data we don't have, such as employer category, work experience, city or bank relationship. The bench will therefore use a fixed synthetic product set that covers all three classes, as planned.

### AD15. The LLM gateway: provider-neutral, budgeted, retried, logged
- **Decision:** callers send `{purpose, model, system, prompt, schema, ...}` and always get back `{ok, data, error, calls, tokens_in, tokens_out}`. Before calling, the gateway counts today's requests in the `llm_calls` ledger (by Pacific day, which is when Gemini resets RPD) and refuses at `llm_rpd`. It retries 429/5xx and network errors, honouring `RetryInfo` / `Retry-After` and otherwise backing off exponentially up to `llm_max_retries`. Every attempt is logged in `llm_calls`.
- **Why:** one place to swap providers. The free-tier quota can't be burned by a loop, and the bench gets exact call and token counts.

### AD16. Crawl runs open and close explicitly
- **Decision:** a `crawl_runs` row is inserted as `running` when a source starts and closed by `finish_run.sql`, which also stores the content hash (on success only), touches `last_seen_at` for an unchanged page, and always returns one row.
- **Why:** a run that dies midway stays visible, a failed source is retried tomorrow because its hash is not stored, and an unchanged page doesn't get its products deactivated after 7 days.

## Workflow B and the optimisation (phase 4, 2026-10-16)

The measurements behind these entries are in `docs/EXPERIMENTS.md`; `make bench` and `bench/llm_experiments.py` reproduce them.

### AD17. The hard filter runs as one SQL join inside Postgres; n8n holds cohorts, never users
- **Decision:** `match_candidates(batch)` is an inlinable SQL function (a set-based join of the batch's users with the active products, plus the compilable filter rules as one anti-join). The write, the cohort step and the bench all call it, so the rules exist once. n8n receives one row of counts and at most `ceil(uncached cohorts / K)` request items.
- **Why:** the deterministic stage is output-bound. Every exact variant evaluates 100k users in well under a second (SQL join, SQL decision cells, a JavaScript bitmask); what costs is moving 100k users (15 MB of JSON) into n8n and 2.37M pairs back out. A naive Code-node loop is also exact, but its working set grows with N x M.
- **Cost:** the matching logic lives in SQL files that the workflow includes, rather than in a Code node a reader sees on the canvas. The workflow's sticky notes point to them.

### AD18. Decide before write: one INSERT per batch, each pair written once with its final status
- **Decision:** cohort verdicts are cached first; then `write_matches.sql` joins the candidates with the cached verdicts and inserts every pair once, already `matched`, `llm_matched`, `llm_rejected` or `pending_llm`.
- **Why:** writing pending rows and updating them after the LLM would write every fuzzy pair twice, and the write is the dominant cost (2.37M rows at 100k users).
- **Cost:** a batch's rows appear only when the whole decision is made, not progressively.

### AD19. matches checks its references per statement, not per row
- **Decision:** the three row-level foreign keys on `matches` are replaced by `AFTER INSERT/UPDATE … FOR EACH STATEMENT` triggers that anti-join the transition table against `users`, `loan_products` and `ingestion_batches`, raising `foreign_key_violation`. Deleting users cascades to their matches; deleting a product or batch that matches still reference is refused (migrations 005 and 006).
- **Why:** at 100k users a row-level FK costs about 5 µs per row per key. The write took 51 s with all three, 22–24 s with any one, and 12 s with the set-based check, which gives the same guarantee on insert and update.
- **Cost:** integrity lives in triggers, which `TRUNCATE` bypasses (only the bench truncates, and it truncates `matches` too). The checks don't take the row locks a real foreign key takes, so a parent row deleted concurrently with the insert can slip through; nothing in the pipeline deletes parents.

### AD20. New users take a plain INSERT; only returning users go through the upsert
- **Decision:** `write_matches.sql` finds the batch's users who already have match rows (one primary-key probe per user). Their pairs go through `ON CONFLICT DO UPDATE`; every other pair takes a plain `INSERT`. Rows are inserted pre-sorted by the primary key, `work_mem` is raised for this statement only, and the Write matches node retries once.
- **Why:** `ON CONFLICT` probes the primary key a second time for every row (speculative insertion). Splitting halved the write at 10k users (2.98 s → 1.52 s, with the two smaller changes) and cut it from 37.7 s to 22.9 s at 100k.
- **Cost:** if another batch writes the same user between this statement's snapshot and its insert, the plain INSERT fails on the primary key and rolls back. The retry sees the committed rows and takes the upsert path.

### AD21. The batch index on matches is BRIN
- **Decision:** `matches_batch_brin` (BRIN on `batch_id`, 32 pages per range, autosummarize) replaces the B-tree on `(batch_id, status)`.
- **Why:** a batch's rows are written by one statement, so they sit in consecutive pages. The B-tree added one index entry and about 80 bytes of WAL per row: at 100k users WAL fell from 960 MB to 766 MB, the write from a median 39 s to 24 s, and the per-batch count got faster too.
- **Cost:** rows of returning users updated in place can widen a range's summary; lookups stay correct (BRIN is lossy and rechecks) and only read a little more.

### AD22. Fuzzy criteria: threshold cohorts judged in one call, plus a compiled rule that can only veto
- **Decision:** the LLM stage has two judges. (1) The cohort judge: pending (pair, criterion) rows collapse to cohorts banded at the product's own thresholds (`COHORT_BANDS=threshold`), 25 per call (`LLM_BATCH_K=25`), cached by (criterion, signature). (2) The rule judge: each distinct criterion is compiled once into clauses tagged eligible / ineligible over the exact facts, with everything else left uncertain (`MATCH_RULES_ENABLED=true`). The cohort verdict decides; a rule clause that rejects the user's facts can turn an *uncertain* verdict into *ineligible* (a veto). A rule never vouches, and any disagreement keeps the pair as "may qualify".
- **Why:** 14 strategies were scored against a per-pair oracle (exact facts, one question per call) on the same 109 pairs. A false drop (oracle keeps, strategy rejects) hides a product, so zero false drops is the first requirement. Only four strategies met it: rules-only (0 calls, catches none of the oracle's 14 rejections), threshold cohorts (1 call, 6/14), coarse cohorts asked one at a time (43 calls, 8/14) and threshold + rule veto (2 calls, 10/14, and the best three-way agreement, 0.706). Every finer banding batched K > 1, per-user calls and plain compilation dropped pairs the oracle keeps (4–26 false drops). The two judges see different things: cohorts decide employment exactly, rules state numeric cut-offs the wording gives ("minimum credit score 720"). Validated on a held-out sample stratified by the decision (docs/EXPERIMENTS.md).
- **Cost:** one compile call per new criterion on top of the cohort calls, both cached forever (0 calls on a rerun). Since AD25 both are made by the warm step after a crawl, not by a batch. Criteria phrased relative to a product ("comfortably exceeds the minimum") can't be compiled and stay uncertain.

### AD23. A replayed batch rewrites nothing
- **Decision:** rows of returning users whose decision is unchanged are filtered out before `ON CONFLICT` by a read-only anti-join, and the upsert's `WHERE … IS DISTINCT FROM` repeats the test. `notified_at` is reset only on rows whose decision changed.
- **Why:** a replay (the sweeper re-claiming a stuck batch, or `make replay`) rewrote every row: 89 s against 34 s for the first write at 100k users, and it reset `notified_at`, which would have emailed users again about matches they already know. Now 10.4 s and no re-notification (V29).
- **Cost:** a re-upload of the same user in a new batch still updates every row (its batch id changes), which is intended: it is a new batch.

### AD24. The Code-node runner stays warm
- **Decision:** `N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT=0`.
- **Why:** the runner exited after 15 idle seconds, so the first Code node of almost every webhook run waited 2.2 s for a cold start (V25), the largest delay in Workflow B after the LLM call and the write.
- **Cost:** one idle Node.js process in the runners container (tens of MB).
- **Update (2026-10-17):** the runner still starts lazily, on the first Code node after an n8n restart (1.1-1.9 s on the first batch). `make bootstrap` now ends with one signed webhook for a batch that doesn't exist: Workflow B runs "Read token claims" and claims nothing, so the runner is up before the first real batch.


### AD25. The match cache is warmed after every crawl, so batches don't wait for the LLM
- **Decision:** Workflow A ends by calling the "Warm match cache" sub-workflow (`lnWfMatchWarm001`). `cohort_space(mode)` (migration 008) enumerates every (criterion, signature) the active products can produce, from the products alone; the sub-workflow judges the ones without a cached verdict and compiles the criteria without a rule, through the same Code nodes, LLM gateway and SQL as Workflow B. Workflow B keeps its own lazy path for anything warming missed.
- **Why:** the first batch after a product change waited for Gemini, 7–15 s per call on the free tier and up to a minute when it answers 503. Under threshold banding the space is exactly what batches produce (19 cohorts for the synthetic products at every size; a test asserts the equality, and coverage for every mode), so warming moves those 2 calls from the batch to the crawl at no extra cost. Through n8n on the synthetic products: the warm step made 2 calls; the next 10k batch made none and was matched in 3.05 s (`make warm-demo`).
- **Cost:** one query per crawl when nothing changed (6 ms). Under coarse or fine banding the space is larger than what real users produce (47 vs 43, 211 vs 123 cohorts), so warming would judge some cohorts nobody needs. A failed warm step never fails the crawl.
## Workflow C and the region (2026-10-17)

### AD26. Workflow C: one statement decides and records everything; n8n holds at most EMAIL_CAP emails
- **Decision:** `plan_notifications.sql` is one statement that:
  - reads `app_config` and checks the email switches (mode, aliases, cap, sender, rate). If any is unsafe it records nothing and returns `config_error`, and the "Check email settings" Code node stops the workflow with that reason;
  - claims the batch and records a `notifications` row for every user with a new match. In `test_aliases` mode the first `EMAIL_CAP` users by user id are `pending`, with the aliases in order; everyone else is `suppressed`. `users.email` is never selected;
  - builds the content for those `EMAIL_CAP` users only. Each (lender, product) appears once, preferring the lender's own page over an aggregator's;
  - closes the batch (`notified`) when nothing is pending.

  Then Render emails (HTML-escaped; "you meet the published criteria" and "you may also qualify", with what to check first), Check recipients (every recipient is an alias, used once; at most `EMAIL_CAP`; sender is `SES_SENDER`), and one email at a time: SES send, record the result, pause for the SES rate. The last recorded email closes the batch and writes the `notify` metric.
- **Why:** the decision is atomic, so a rerun finds everyone recorded and sends nothing. The CSV address can't reach n8n. The cap holds in two places. In suppressed mode the same emails are rendered as previews (`make email-preview`), so the content can be checked without sending.
- **Cost:** the safety rules exist twice (SQL and the Code node), and tests check both. Pending rows left by a crashed run are never retried, because SES may already have sent them; a rerun closes the batch and they show up in the `notify` metric as `pending`.

### AD27. AWS stays in ap-southeast-2 (Sydney); the region is one variable, and the workflows keep their database round trips few
- **Decision (owner, 2026-10-17):** every AWS resource lives in `AWS_REGION=ap-southeast-2`. The region is set once in `.env`. The n8n SES credential (`scripts/n8n_credentials.py`), `app_config.aws_region` and the Lambdas read it from there, and `infra/serverless.yml` will too (`${env:AWS_REGION}`). Workflow B and C statements were merged so a batch costs few round trips:
  - B, cached path: claim + config + uncached cohorts is one statement, and write + finish is another. That is **2 instead of 5**.
  - C: **1 instead of 3** when nothing is sent, plus 1 per email sent.
  - LLM gateway: config + today's usage is one statement, so **2 per call instead of 3**.
  - Warm step: **1 instead of 2**.
  - Workflow A is unchanged (10 for three sources): it runs once a day, and its time goes on page fetches, polite pauses and LLM calls.
- **Why Sydney:** the account was opened with AWS's simplified sign-up, which pins a project to one region by country, and an Organizations SCP denies every other region (B1). Moving needs the Paid plan plus advanced features that can't be undone, and the brief expects no cloud costs.
- **Which hops cross Bengaluru ↔ Sydney** (n8n runs on the owner's laptop in Bengaluru):

  | Hop | Crosses? | Per batch |
  |---|---|---|
  | Browser → presign Lambda, browser → S3 presigned upload, browser → status Lambda | yes (the user is in India) | 1 + 1 + polling |
  | S3 → ingest Lambda → RDS; ingest → notify (Lambda destination); notify → SSM | no, all in-region | — |
  | notify Lambda → Cloudflare → cloudflared → webhook-gw → n8n | yes, once | 1 request |
  | n8n → RDS | yes | 3 round trips (B 2 + C 1), +1 per email; +1 new connection (≈5 round trips) after 10 idle seconds (V32) |
  | n8n → SES API (`email.ap-southeast-2.amazonaws.com`) | yes | 1 call per email, `test_aliases` only (≤ 2) |
  | n8n → Gemini | not AWS: unaffected by the AWS region | 0 on a warm cache |
  | Bench and tests (psql + node against local `app-db`) | no: everything on the laptop | — |

- **Measured** (`make timing ARGS="--run 10000 --compare 144"`, 2026-10-17, 10,000 fresh users, local `app-db`, `bench/results/timing-local.json`):
  - **Network from this machine:**
    - TCP handshake to `sts.ap-southeast-2`: **388 ms** median (p90 426). The other Sydney endpoints (S3, EC2, SES) took 374–432 ms.
    - To `sts.ap-south-1` (Mumbai): **33 ms** (p90 39); to Singapore: 51 ms. This network reaches Australia by a long route.
    - Local `app-db`: `SELECT 1` 0.2–1.4 ms, fresh connection 6.6 ms.
    - Webhook through the cloudflared tunnel: 49 ms, against 30 ms direct, so the tunnel itself adds 19 ms.
  - **Round trips per batch:** 3 (B 2, C 1). Execution 144, before the merge, made 8 (B 5, C 3).
  - **In-region work:**
    - batch load: 0.21 s;
    - SQL execution as the server timed it: 3.09 s. The local database holds 4.26M match rows from earlier test runs, which makes the write slower than on a fresh database (1.1 s at 10k, EXPERIMENTS section 5).
  - **n8n on the laptop:** Code nodes and scheduling 0.22 s. Webhook to notified: 4.05 s.
  - **Cross-region cost if RDS were in Sydney and n8n stayed here:**
    - 3 × 388 ms = **1.17 s** of round trips;
    - plus **1.94 s** for the new connection a webhook usually needs (5 × 388 ms);
    - plus the tunnel hop from the Sydney Lambda, ≈ **0.41 s** (region RTT plus the tunnel).

    That is about 3.5 s on top of about 4 s of work. With the 8 round trips from before the merge it would be about 5.5 s.
- **Production fix:** run n8n in the same region as RDS, where a round trip takes about 1 ms and the notify Lambda reaches n8n without crossing an ocean. Alternatively, deploy to ap-south-1 with a standard account: 33 ms per round trip from Bengaluru, so about 0.3 s for the same run, connection included. Either way only `AWS_REGION` changes.
- **The bench is unaffected:** its numbers (EXPERIMENTS) come from the local `app-db` through psql and node, so they don't depend on the region.
- **Cost:** no money. Latency only while n8n and RDS are on different continents. The SES identities were re-verified in Sydney.

### AD28. The local database is configured for the write path
- **Decision:** `app-db` runs with `shared_buffers=1GB`, `wal_compression=lz4`, `max_wal_size=8GB` and `checkpoint_timeout=15min`, set in `docker-compose.yml` (not `ALTER SYSTEM`, so the settings are visible and reproducible). `fsync` and `synchronous_commit` stay on.
- **Why:** the write slows as `matches` grows. With 5.5M rows its primary-key index (533 MB) was four times the default 128 MB buffer cache, and user ids are random UUIDs, so each of a batch's 236k inserts touched a random index page. The 10k-user write, measured on that table, rolled back, 4 runs each:
  - defaults: median 7.65 s (3.4-10.3);
  - with WAL compression: 5.20 s, and WAL per batch fell from 106 to 83 MB;
  - also a larger WAL budget: 5.47 s;
  - also the 1 GB buffer cache: **3.10 s** (2.5-4.1).

  On a fresh, small table the settings measured neutral (10k: 1.8 vs 1.6 s; 100k: 23.5 vs 26.8 s, inside the run-to-run noise), apart from 40% less WAL.
- **For RDS (phase 3):** set `wal_compression` in the parameter group. A `db.t4g.micro` has 1 GB of RAM, so its buffer cache can't hold a large `matches` index; at scale that needs a bigger instance or fewer rows per batch (EXPERIMENTS section 11).
- **Cost:** 1 GB of the Docker VM's memory for `app-db`.

### AD29. LLM request size and parallelism are planned per model, from its own latency
- **Decision:**
  - The gateway takes all of a batch's requests in one call. The HTTP node sends them `llm_concurrency` at a time (`LLM_CONCURRENCY`, default 2), with a pause between groups that keeps the account under `llm_rpm`.
  - Each request is budget-checked on its own, and every call's duration is now logged.
  - `llm_batch_k` becomes a **cap**: the most cohorts per call validated for accuracy with this prompt (25 on `gemini-3.5-flash-lite`).
  - `build_cohort_requests.js` plans K from the match model's latency, fitted from its own `llm_calls` rows (duration against output tokens, last 200 answered calls).
- **Why:**
  - **One request at a time,** a batch costs (requests × fixed latency) + (tokens × per-token latency), so the fewest requests win: K = the cap, as before. Requests are byte-identical in that case, so all 14 cached bench strategies replay with 0 calls.
  - **With parallel slots and output-bound latency,** smaller balanced requests finish sooner. The planner picks the K whose requests, packed onto the slots, finish first, and prefers fewer requests on a tie, since each request repeats the system prompt and costs tokens.
  - **On Gemini's free tier** latency is flat: 343 calls, R² 0.01, a median of 7-10 s up to 885 output tokens, and 22.5 s at 1,902. The planner treats any fit with R² below 0.3, or fewer than 20 calls, as flat, so K stays at 25 there.
  - **A faster paid model** shows output-bound latency in its own ledger, and the same code then splits the work across the slots. The default is one request at a time.
- **Measured through n8n:**
  - **warm step:** 2 requests (19 cohorts, 12 criteria) ran side by side, both done in 3.9 s;
  - **a batch that had to judge its own cohorts:** both requests in 5.2 s, webhook to matched 8.4 s, where the same work took about 23.5 s when the calls ran back to back.
- **Would Claude, or any faster model, help?** Not at match time: batches make 0 LLM calls, so the model doesn't matter there. It shortens the crawl's extraction call and the warm step, both off the user's path. Claude also has no free tier (Considered and rejected).
- **Cost:** a gateway that handles many requests per call, with a result node that reads every retry pass (V36).

### AD30. Products are identified by lender and name within a source, not by exact wording
- **Decision:** `product_identity(lender, product_name)` (migration 010) compares names without punctuation, without "Bank", "Ltd", "Limited", "powered by", and without repeating the lender's own name: "HDFC Personal Loan" and "HDFC Bank Personal Loan" are both `hdfc|personal loan`. `loan_products.identity_key` is generated from it, and `(source_id, identity_key)` is unique. The crawl upsert matches on it, so a re-extraction that names a product differently updates that product (taking the new name and logging any criteria change). Two entries of one extraction with the same identity are one product, and the one with more hard-filter fields is kept, as the validator already did for exact repeats. Workflow C's email de-duplication uses the same key.
- **Why (open issue O1, found 2026-10-17):** keyed on the exact name, each re-extraction of an unchanged page could add a copy. Four were live: Axis, TurboLoan/Chola and Kotak on top of the HDFC case that surfaced it. Every user matched both copies, and an unchanged page refreshed `last_seen_at` for both, so the 7-day rule never retired either. Migration 010 kept the newest reading of each (the current prompt and validation; for HDFC, the one that ranks rather than filters on "720 preferred"): 33 active products became 29, and a 10k batch went from 236k to 214k pairs.
- **Cost:** none measurable: the key is computed once per product write. Two genuinely different products of one lender on one source can't share a normalised name (no collision among the 33).

### AD31. Matches are stored as one row per user (user_matches), with `matches` as a per-pair view
- **Decision:** migration 011 replaces the `matches` table, one row per (user, product), with `user_matches`, one row per user:
  - `product_ids` and `statuses`: parallel arrays sorted by product id;
  - `details`: a map of the few pairs with a score, a reason or caveats (5% of pairs on the real products);
  - `to_announce`: replaces the per-pair `notified_at`;
  - `decided_by` follows from the status.

  `matches` remains as a read-only view with the old per-pair columns, so every reader of pairs (the bench's recall check, the end-to-end check, the docs' queries) is unchanged. Integrity stays statement-level (AD19): users, batches and the distinct product ids are checked once per statement, and the parent guards look at `user_matches`.
- **Why:** the per-pair write was the largest cost of every batch. Each pair was a heap tuple, an entry in a primary key on a random UUID, and WAL for both. Measured on the same batches, layouts interleaved, with the per-pair content compared afterwards (0 differences in every run, up to 2,147,170 pairs; EXPERIMENTS section 12):
  - **write:** 10k 1.16 → 0.37 s, 100k 25.5 → 8.3 s (real products);
  - **re-upload:** 10k 3.90 → 0.48 s, 100k 140 → 14 s;
  - **replay:** 2-2.7× faster;
  - **Workflow C:** its plan reads the batch's own rows through BRIN and is 1.1-1.3× faster; its record step is equal (27.6 vs 28.2 ms);
  - **storage** 6-8× smaller and **WAL** 6-8× less.

  Through n8n a 10k batch is matched 0.85-1.36 s after the webhook, against 2.7-3.6 s before.
- **Consequences checked:**
  - **Announcing:** `to_announce` reproduces when a user is emailed. A new batch announces any offers. A rerun of the same batch that changes the decisions announces again, and an unchanged rerun writes nothing. The one difference: a change only to a product's criteria hash, with identical decisions, no longer re-announces, since nothing the user would see changed.
  - **Per-pair `criteria_hash`:** no longer stored, as no workflow read it; the criteria history keeps the versions.
  - **Ad-hoc questions:** "which users match product X" scans `product_ids`, as the old table did too: it had no index on product_id.
  - **Row size:** the widest row (29 products) is about 0.7 KB, far from the 2 KB TOAST threshold.
- **Cost:** none: less storage and I/O on RDS. The view unnests on read, which measured faster than reading the old table (0.20 s against 1.33 s for a 100k batch).

## Ingestion (phase 2b, 2026-10-17)

### AD32. Ingest validates in one SQL statement; Python only picks columns and streams them
- **Decision:** `services/ingest/core.py` reads the header, picks the seven columns by name (any order, extras ignored, BOM and CRLF accepted), and streams the raw text into `COPY` of a temp table. One statement then does everything else:
  - validates each row with ordered `CASE` checks, so a cast only runs on text already proved castable;
  - keeps the last row of a repeated `user_id` and rejects the earlier ones with that reason;
  - upserts the users and closes the batch.

  Rejected lines, with their reason and the raw values, go to `rejects/{batch_id}.csv` through a `SpooledTemporaryFile`. A redelivered S3 event finds its `(bucket, key, etag)` batch and does nothing. PLAN §5 2b said "validates each row into a typed model"; this replaces that.
- **Why:** an upload can be about 700k rows (the 50 MB presign limit), and per-row Python validation would cost more than everything else. Measured on this machine:
  - 10k rows in 211–251 ms;
  - 100k rows (2% dirty) in 1.9–2.1 s, about 50k rows/s.

  The reject reasons are checked against an independent Python validator (`tests/test_ingest.py`). On a 100k file with 2,036 injected faults, each reason's count equals its fault's count; padded values are trimmed and accepted.
- **Cost:** the validation rules live in SQL, not in a Python model. The Python reference in the tests keeps the two honest.

### AD33. The presign and status Lambdas sign with a stdlib SigV4, not boto3
- **Decision:** `services/common/sigv4.py` builds the S3 POST policy and the RDS IAM auth token with `hmac` and `hashlib`. The ingest and notify Lambdas, which make real API calls (S3, SSM), import boto3 lazily inside the handler.
- **Why:** presign and status sit on the upload page's critical path, and both signatures are offline HMAC chains. `import boto3` plus one client took 122–252 ms here, against 0.6–1.6 ms for the module. Lambda's fractional vCPU makes the gap wider. Tests check that both outputs equal botocore's for the same inputs and frozen time. The POST compares fields, policy and signature under `signature_version="s3v4"`, since botocore otherwise signs POST policies the legacy way.
- **Cost:** about 150 lines that must track SigV4. It's a stable, versioned spec, and the equality tests fail on any drift. The upload URL is the regional endpoint, `https://{bucket}.s3.{region}.amazonaws.com/`: botocore's global form can redirect, which breaks a cross-origin POST.
- **Versions (dev only):** `boto3==1.43.114` and `moto[s3,ssm]==5.2.4` run the offline tests. In Lambda, ingest and notify use the runtime's own boto3 (checked against the runtime docs in phase 3).

### AD34. Ingest's integrity and statistics are per statement, and ANALYZE refreshes only what changed
- **Decision:** migration 013 and `ingest()`:
  - **The `users.batch_id` foreign key becomes one check per statement.** Two `AFTER INSERT/UPDATE … FOR EACH STATEMENT` triggers do it, as AD19 and AD31 do for matches. Unlike those, this check takes `FOR KEY SHARE` on the batch, so a concurrent delete of the batch waits and is then refused, as with a real foreign key (tested). Deleting a referenced batch is refused, and `ingestion_batches.batch_id` can't change.
  - **`users_updated_at` only fires when the statement didn't set `updated_at`.** Ingest's upsert sets it already.
  - **`LOAD_SQL` carries the trimmed values through its CTEs** instead of joining back to staging. `work_mem` is 64 MB for the transaction, and `ANALYZE staging` is gone.
  - **`ANALYZE users (batch_id)` after a batch.** The full `ANALYZE users` runs only when the table has never been analyzed or the batch is over 10% of it, which is when autovacuum would analyze it anyway (`analyze_users()`).
- **Why:** EXPLAIN ANALYZE of a 100k re-upload:
  - the foreign key's per-row trigger took 354 ms (now 28 ms, one call);
  - the `updated_at` trigger took 57 ms;
  - the re-join took 98k index probes (about 270 ms);
  - both sorts spilled 13 MB to disk;
  - the full ANALYZE took 85–270 ms at 160k users, most of it sorting `name` and `email`.

  Ingest went 2514 → 1979 ms at 100k and 594 → 211 ms at 10k (re-uploads). Matching does need `batch_id`'s statistics: without any ANALYZE, a 10k batch matched in 791–952 ms against 534–538 ms. Interleaved through n8n on the same database state, against always running the full ANALYZE:
  - 2k: 587 → 528 ms end to end;
  - 10k: 1445 → 1363 ms;
  - B's match time was identical (130/130 ms and 584/586 ms).
- **Cost:** the reference lives in triggers, which `TRUNCATE` bypasses. `notifications` and `pipeline_metrics` keep their real foreign keys, so `ingestion_batches` still can't be truncated without `CASCADE`. Between a small batch and the next autovacuum, the other columns' statistics can be up to 10% stale.

## AWS stack (phase 3, written and compiled offline 2026-10-17)

### AD35. The stack: plain CloudFormation where the CLIs differ, one role per function, nothing secret in the template
- **Decision:** `infra/serverless.yml`:
  - **CloudFormation for the parts osls and v4 might compile differently:** the bucket and its S3 event, the IAM roles, the URL grants. ARNs are built with `Fn::Join`, not `!Sub`, whose `${AWS::…}` collides with the framework's variables.
  - **One role per function, each with only its own work:**
    - presign: `s3:PutObject` on `uploads/*`, which authorises the browser's POST;
    - ingest: read `uploads/*`, write `rejects/*`, `rds-db:connect` as `lambda_app`, its two destinations;
    - notify: `ssm:GetParameter` on its two parameters;
    - status: `rds-db:connect`.
  - **The RDS master password is an `ssm-secure` reference.** It and the webhook secret are SSM SecureStrings put by `make aws-params`.
  - **RDS is pinned to 17.11** (V16), with `DeletionPolicy: Delete` and `UpdateReplacePolicy: Snapshot`. Logs stay on the instance for 7 days (`rds.log_retention_period`) rather than going to CloudWatch.
  - **Two reproducible zips** from `make lambda-build`: the code with the RDS CA bundle (96 KB), and a psycopg layer (6.1 MB) attached only to ingest and status.
  - **The page's presign call sends `text/plain`,** which makes it a CORS simple request.
- **Why:**
  - Per-function roles hold each function to its own work, and the plain resources behave the same under either CLI: `make sls-package` compiles the file with osls, and `tests/test_infra.py` checks it.
  - With `DeletionPolicy: Delete`, a teardown leaves no billed snapshot; with `Snapshot` on replacement, an accidental replacement keeps the data.
  - Keeping the logs on the instance avoids CloudWatch ingestion charges.
  - The layer's wheel ships 5 MB of C source and stubs Lambda never reads, so they are left out. Presign and notify don't load the layer at all.
  - `application/json` would make the browser send a preflight `OPTIONS` before the first presign, a full extra round trip to Sydney (about 390 ms from India, AD27). The status GET and the S3 POST are simple requests already.
- **Cost:** about $0 on the Free plan, apart from RDS's public IPv4 (AD8). The stack holds a VPC, two subnets, an internet gateway, one free S3 gateway endpoint, two security groups, RDS, the bucket, a queue, four functions and a layer. The test fails if a NAT gateway, interface endpoint, proxy, load balancer, EC2 instance, Secrets Manager secret or provisioned concurrency appears.

## Upload page (2026-10-17)

### AD36. The upload page is a Next.js static export, one card that follows the batch to the end
- **Decision:** `ui/` is a Next.js app (App Router, TypeScript, Geist from `next/font`) built as a static export into `ui/out`. `make ui` builds it and serves it with the local stand-ins, or with `UI_TARGET=aws` points it at the deployed stack through `/config.js`. `make ui-dev` runs `next dev`, which forwards `/config.js` and `/api/*` to the stand-ins.
  - **The page:** a product header (icon, name, tagline, three facts, a sample CSV), then one card: drop or choose a file, then four live steps (upload, check every row, match loans, notify customers) with each step's time, and a result with four numbers. A three-line "How it works" and a two-line footer. White, navy ink, one blue for actions, mint only for "done".
  - **The flow:** the file is checked in the browser by ingest's own rules (the first 64 KB: required columns, size, type), while the presign request is already in flight. The file then goes straight to S3 with progress, and the status is polled at 250 ms, then 700 ms, then 2 s, with backoff and a reconnect notice. A refused file names what's wrong, and a failed upload offers a retry without choosing the file again.
  - **New in the status call:** `matching.customers_with_offers` (counted in the write statement, from rows it already holds), `matching.products` and `email_mode`, so the page can say "All 1,905 customers qualify for a loan" and describe emails truthfully ("sending is switched off" or "held back in test mode").
- **Why:**
  - **A static export:** the page has no server code, so it can be served by `make ui` or any static host. It also avoids the server attack surface that V41 patched.
  - **Fewer waits on Sydney (AD27):** checking and presigning overlap, presign is a simple CORS request, connections open on page load, and all numbers arrive in one status call.
  - **The design:** ClickPe's own site (navy #001143, royal blue, large radii, short benefit-led copy) set the tone, and a Product Hunt-style product header frames the card, without copying either.
- **Checked:**
  - 14 logic tests (`ui/lib`): every status to step mapping, the copy, Indian digit grouping, the CSV header rules.
  - A real-browser run through the local stack: idle, a refused file, in progress, finished, and phone width. It found no console errors and no horizontal overflow, and a 2k-row file finished in 1.2-2.7 s from drop to done.
- **Cost:** about 860 KB of static assets, cached for good under `_next/static`. Node 24 and an `npm ci` are needed to build it.

### AD37. Eligibility: stated score minimums filter, products without criteria match nobody, one lender is one product
- **Decision:**
  - **A stated minimum credit score is a hard filter.** The extraction prompt (`extract-v3`) says a stated requirement wins over "ideal" or "generally good" wording. On a lender's own page, `validate_products.js` also reads the text: a sentence stating a score as a requirement sets the minimum, the lowest such value, even if the model called it preferred. Aggregator pages are left to the model, because "most lenders require 650" is not one lender's rule.
  - **A product that publishes nothing we can check matches nobody** (migration 014, `has_criteria`). This means no threshold, no allowed employment, and no fuzzy or compilable criterion. Such products stay in the catalogue.
  - **One lender under two names is one product** within an extraction: a single-word name equal to the other's initials ("IIFL" and "India Infoline Finance Ltd.", "PNB"), with product names compared without either lender's words.
  - **The page says what a match means:** "meet at least one lender's published criteria … a match means a customer may qualify; it is not a loan approval". The email already said this.
- **Why:** the owner saw "100% of customers qualify". Measured on the sample's 10,000 customers:
  - HDFC's page says "you need a minimum credit score of 720", but the score was stored as a ranking hint, so no product filtered on score.
  - Three products with no extracted criteria matched everyone.
  - Each customer had 15-29 products.
- **Result** (`make match-e2e`, recall and precision 1.0000):
  - 26 matchable products instead of 29;
  - 194,278 pairs pass the hard filter instead of 214,642, and 6,608 are now rejected by the LLM step;
  - 12-26 products per customer (median 18) instead of 15-29 (median 20);
  - HDFC's direct product goes to 1,847 customers instead of 3,314.

  Every customer still meets at least one lender's criteria. That is true of this file: income ₹25k-1.5L, scores 650-800 and ages 22-50 clear about 12 products' published limits.
- **Second pass, same day:** `users.csv` still showed 100%. Three more readings were too generous, each backed by the page text:
  - **A source's general score requirement now applies.** BankBazaar's "general requirements" table says "Credit Score | CIBIL score of 750 or more", and its per-lender rows give no score. That 750 now applies to every listed product that states criteria of its own but no score. The FAQ's "most lenders require 650 to consider an application" is about being considered at all, so sentences about what lenders "consider" are skipped.
  - **Concession tiers no longer set the floor.** An income tier for the bank's own employees or salary-account holders (Kotak: ₹20,000, ₹25,000) is a concession; the floor is the general public's tier (₹30,000), and the lower tiers stay caveats.
  - **A successful re-extraction retires what it no longer returns.** Before this, an `unchanged` run marked every active product as seen, so a product the last extraction dropped kept its old, looser criteria indefinitely. Four did; they were retired.
- **Result on `users.csv`, through the page:**
  - 4,920 of 10,000 customers (49%) meet at least one lender's published criteria, across 24 products, each with a minimum score;
  - score 750+: everyone (median 24 products if salaried, 14 otherwise);
  - 720-745 and 685-715: salaried only (HDFC, Kotak);
  - 650-680: no one.

  `make match-e2e`: recall and precision 1.0000.
- **Cost:** the score guard trusts the page's wording. A page that states a requirement for one segment only is read as the lender's minimum, and an aggregator's general requirement is applied to lenders that may in fact accept less. An LLM miss now retires a product until the page changes and is re-extracted; the crawl reports how many it retired.

## Review and demo preparation (2026-10-18)

### AD38. An unreadable file is refused as a whole, never retried
- **Decision:** `ingest()` turns errors that come from the file's bytes into `IngestError`: a
  truncated or corrupt gzip (`EOFError`, `BadGzipFile`, `zlib.error`), a field over csv's
  128 KB limit (`csv.Error`), and NUL bytes, which Postgres text can't hold (`psycopg.DataError`
  while copying). The handler then records a failed batch with the reason, as it already did
  for missing columns.
- **Why:** before, these raised past the handler. Lambda retried the event twice, which can
  never succeed, then sent it to the DLQ, and no batch row existed, so the page polled
  "uploaded" until the user gave up. The page checks only the first 64 KB, so a file can pass
  it and still be unreadable further down.
- **Cost:** none. Database and network errors still raise and are retried. Tests: three
  parametrised cases in `tests/test_ingest.py`. The deployed ingest Lambda gets this on the
  next `make deploy`.

### AD39. `make demo-reset`: every workflow's next run takes its full path, and nothing else changes
- **Decision:** `scripts/demo_reset.py` undoes exactly the guards that make a repeat run short.
  - **Default:** it forgets one source's page hash (`hdfc-bank`: the smallest page, and the one
    whose product has the fuzzy criterion), so A re-extracts it while the other sources show the
    unchanged skip. It also drops the cached verdicts and compiled rules for the cohorts the
    active products can produce (`cohort_space()`), so the warm step judges them again.
  - **`--cold-batch N`:** it drops the same cache rows and loads N new users as an `ingested`
    batch, so B's "Run manually" claims it and takes the LLM branch itself, then calls C.
  - It prints the plan (including the email mode, so it's clear whether C will send), asks for
    confirmation, and changes everything in one transaction. `--target rds`, `--dry-run`.
- **Why:** the owner saw B's manual run end at "Nothing waiting" and asked how to show the full
  path. Every short path is a guard working as intended (unchanged page, warm cache, no waiting
  batch, already notified). The demo needs the long paths without weakening any guard.
- **Checked:** through n8n against a scratch copy of the catalogue with email suppressed:
  - A: hdfc-bank changed with 1 LLM call; bankbazaar and kotak unchanged.
  - The warm step judged 1 cohort and 1 criterion.
  - B: Claim → LLM needed? true → Judge → Store → Write → C.
  - All five workflows succeeded.

  `tests/test_demo_reset.py` checks that only active-space cache rows and the named hash
  change, that a dry run changes nothing, and that an unknown source is refused.
- **Cost:** about 2 Gemini requests per reset (plus 1 per forgotten page) on the next run. It
  deletes cache rows, which the next run rebuilds; it never deletes users, products, matches or
  notifications.

### AD40. The upload page shows the funnel, the stack it talks to, and how each step works
- **Decision:**
  - The result card adds **"How N possible checks were narrowed down"**: customers × products,
    then the pairs that pass the published criteria, then the pairs needing judgement, then the
    AI calls for this upload. Bars are to scale.
  - Every number comes from fields the status call already returned (`rows.valid`,
    `matching.products`, `matching.pairs`, `by_status`, `llm_calls`), so the deployed status
    Lambda needs no change.
  - A top bar with a badge reads `config.js` (`target`, `region`, written by
    `scripts/ui_server.py`): "Live on AWS · ap-southeast-2" or "Local demo".
  - Below the card: four "how it works" steps, each with the component behind it (S3 presigned
    POST, Lambda + COPY, n8n + SQL + Gemini, n8n + SES), the three lender sources, and a short
    FAQ ("a match is not a loan approval", file format, who gets emails, double delivery).
- **Layout (revised the same day, after the owner's review):**
  - The nav, sections and footer share one 1000px container, so every left edge lines up.
  - The hero is centred: an eyebrow pill, a navy headline with one blue phrase, a subtitle and
    a check-mark trust row. The 760px upload card sits directly under it.
  - The hero's repeated logo and the floating "Sample CSV" button are gone; the sample link
    now sits inside the drop zone.
  - Each section gets an eyebrow and an h2, with even four- and three-column grids. The FAQ
    uses a two-column layout.
  - Numbers use the sans font with tabular figures; mono is kept for column names only.
- **Why:** the owner asked for a more complete, product-like page that shows reviewers what
  the assignment is about, without losing the minimal design. The funnel puts the optimisation
  in front of every upload.
- **Checked:** headless Chrome over the DevTools protocol, a real file input and the local
  stack on a scratch database with email suppressed:
  - 10,000 rows from drop to done in 1.0 s;
  - funnel 2,40,000 → 61,937 → 1,840 → 0;
  - no console errors, no horizontal overflow at 390 px.

  `funnel()` has a `node --test` case.

### AD41. Database logins are hedged and never cancelled, and the in-VPC Lambdas stay logged in
- **Incident (2026-10-18, 17:16):** the first upload after a redeploy took 1 min 42 s instead of
  about 13 s:
  - the fresh ingest container's three login attempts (3, 5 and 10 s, V44) all timed out,
    although a raw TCP connect to RDS took 2 ms;
  - the invocation failed, and Lambda's own retry ran **64 s later**, logging in at once;
  - everything after the login took 15 s, as usual.
- **Root cause:** inside the VPC a login's time is RDS's server-side IAM check (V47). Every
  check that reached the auth proxy succeeded; the checks were just slow. **The short-attempt
  ladder cancelled each slow check and started a new one**, so while a check took longer than
  10 s, no attempt could ever finish.
- **Decision:**
  1. **Hedged logins** (`services/common/db.py`). The first attempt waits for the whole budget.
     Attempts 2–5 start *beside* it at 4, 10, 20 and 35 s, in case its path is stuck rather than
     slow. The first to finish wins; late ones close themselves. If every attempt fails fast
     (e.g. RDS still starting), a new one starts after 1 s, until the budget is spent.
  2. **The budget comes from the Lambda's remaining time.** Ingest gets up to 120 s (timeout
     300 s, with 60 s kept for the file). Status gets up to 50 s (timeout 60 s).
  3. **A cached connection is checked with one round trip** (about 1 ms) before reuse; a dead
     one, e.g. after an RDS restart, is replaced instead of failing the request.
  4. **Ingest logs in before it opens the S3 stream**, so a slow login can't time the download
     out.
  5. **Keep-warm.** An EventBridge rule invokes ingest and status every 5 minutes with
     `{"warmup": true}`. The handler confirms its cached connection, or logs in, off the user's
     path. With RDS stopped, a 3 s TCP check fails and the handler returns without raising: no
     retries, no dead-letter message, no billed minutes. `make deploy`, `make rds-start` and
     `make use-rds` end with the same warm-up (`make warm`), so even the first upload after
     any of them finds a logged-in container.
  6. **notify skips a ping before importing boto3.** Pings reach notify through ingest's
     onSuccess destination.
- **Checked:**
  - 6 tests against a real Postgres:
    - a login slower than the first hedge is waited for;
    - a stuck attempt is hedged, and the late one closes itself;
    - fast failures retry until the budget runs out;
    - a dead cached connection is replaced;
    - warm-ups never raise, and return within 4 s with the database down;
    - the budget follows the remaining time.
  - An infra test pins the schedule, the permissions and the timeouts.
  - Live, right after the deploy, with every container fresh, a one-row upload from India
    (`make aws-upload`, which sends no email) finished in **12.7 s**, against 1 min 42 s before
    the fix. Warm, it took **7.3 s**. `make warm` reports both functions logged in.
- **Cost:** about 17k extra invocations a month (plus notify's skips), inside the free tier, and
  about 1 ms per request for the liveness check. RDS's IAM check can't be made faster from our
  side. If it ever takes 60 s, that upload waits 60 s, but it no longer fails into Lambda's
  64-second retry, and keep-warm makes this rare on the user's path.

## Crawl sources

| Source | Why it was chosen |
|---|---|
| **BankBazaar** `/personal-loan.html` plus `/personal-loan-eligibility.html`, treated as one source whose pages are concatenated before hashing | Server-rendered. A 15-row bank rate table, about 14 lender cards with age, income and city criteria, and a 16-row bank eligibility table (age, work experience, monthly income). robots.txt allows both pages for `*`. |
| **HDFC Bank** `hdfc.bank.in/personal-loan/eligibility-criteria` | A direct lender page with criteria the LLM has to classify: an employer category (fuzzy or unknown_field) and work experience (unknown_field). |
| **Kotak** `kotak.bank.in/.../personal-loan/eligibility.html` | Income tiers set by the customer's relationship with the bank. It tests the "most permissive tier becomes the filter, the tier becomes a caveat" rule. |
| Backup: **Hero FinCorp** `/personal-loan-eligibility` | An NBFC page with "CIBIL 725+ preferred" (compilable) and residence stability (unknown_field). |

**Rejected:**
- **Paisabazaar:** robots.txt allows it, but Akamai returned 403 after about 5 requests.
- **ICICI:** the criteria exist only as embedded calculator JSON.
- **IDFC First:** returned 500.
- **Bajaj, Tata Capital, IIFL, Fibe, MoneyView:** their eligibility URLs return 404 because the pages moved.

**Etiquette:** the crawler checks robots.txt in the workflow, identifies itself in the User-Agent, waits between requests, and runs once a day. `CRAWL_MODE=fixtures` is the default, so tests and the demo never depend on a live site. Aggregator terms of service may restrict automated access even where robots.txt allows it. At this volume (at most 4 pages a day) the risk is low, and it's stated here.

## Considered and rejected

To be expanded in phase 5 with numbers from the bench.
- **Embeddings or vector similarity:** the data is structured numbers, so there is nothing semantic to retrieve.
- **A classifier trained on LLM labels:** it would only approximate rules that can be written down exactly.
- **Per-user LLM calls with better prompts:** the cost still grows with users × products.
- **Claude as the LLM provider (2026-10-16):** structured outputs guarantee schema-valid JSON, but the Claude API has no free tier. The estimated project total would be about $3–60 depending on the model. The owner chose Gemini's free tier to keep spend at zero. The gateway sub-workflow can still add a provider branch later.
- **Serverless v3 without a license:** it's end-of-life. osls is the maintained fork if the v4 login becomes a blocker (V7).
