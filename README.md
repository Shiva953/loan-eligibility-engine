# Loan Eligibility Engine

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/banner-dark.png">
  <img src="docs/banner.png" width="100%" alt="Architecture: the browser gets a presigned POST and uploads the CSV to S3; an ingest Lambda in the VPC loads it into RDS Postgres; notify sends a signed webhook through a tunnel to n8n, where Workflow B matches and Workflow C emails through SES; Workflow A crawls lender pages and an LLM gateway calls Gemini. 2,40,000 possible checks become 0 AI calls per upload.">
</picture>

Upload a CSV of customers and the system works out which personal loans each of them is likely
eligible for, using the criteria lenders publish on their own websites, then emails the people
who have a match.

The AWS side takes care of ingestion. The browser uploads the file straight to S3 with a
presigned POST, and a Lambda loads it into RDS Postgres. Everything after that runs in a
self-hosted n8n:

- Workflow **A** crawls lender pages every day.
- Workflow **B** matches each new batch of users against the products.
- Workflow **C** sends the emails.

For the 10,000-row sample and 24 products, the 2,40,000 possible (user, product) checks come
down to a single question for the LLM. That question is answered right after the daily crawl, so
an upload makes no LLM calls at all. Matches agree with a brute-force check of every pair at 1k,
10k and 100k users.

## Demo video

**[Watch the full demo](https://drive.google.com/file/d/1qsIigkDdpJJgSd1ztAXSMapAdR7U-8UC/view?usp=sharing)** (9 min 44 s)

| Starts at | Part |
|---|---|
| 0:00 | The problem: 2,40,000 possible checks, and why an upload needs 0 AI calls |
| 0:38 | The upload page |
| 0:54 | Architecture |
| 1:37 | Event-driven ingestion: presigned upload, one SQL statement, duplicates and failures |
| 2:14 | Workflow A, loan product discovery: how it crawls and extracts |
| 2:50 | Workflow A live in n8n, then the warm step |
| 3:59 | The LLM gateway |
| 4:17 | The optimisation treasure hunt: funnel, cohorts, two judges, warming, the oracle, scaling |
| 6:34 | Workflow B, user-loan matching: the design, then a live run through the LLM branch |
| 7:37 | Workflow C, user notification: the design, then the live run and the SES sends |
| 8:16 | The whole pipeline, live on AWS: `users.csv` uploaded, the funnel, the B and C runs |
| 9:03 | The email received in the test inbox |
| 9:20 | The engine, measured |

The live footage in the demo comes from these five recordings, linked here for reference:

1. [01 · Opening](https://drive.google.com/file/d/1KrckeEAZKq_fdv4llOZ0cupT43UFtKgs/view?usp=sharing): the live upload page, connected to AWS
2. [02 · Workflow A](https://drive.google.com/file/d/1lSw08UjPGzfqIatd2oUKUruNB_QbbfEL/view?usp=sharing): a crawl run, with the unchanged-page skip, LLM extraction and the warm step
3. [03 · Workflows B and C](https://drive.google.com/file/d/1XjSdmAUP8gOaMQrKYykILC1QWnR1rLAY/view?usp=sharing): every branch of B, the LLM path included, then C
4. [04 · End to end on AWS](https://drive.google.com/file/d/1ihUo8At-k8U36XBNKOnuBH5JdYchwEyn/view?usp=sharing): `users.csv` uploaded, the funnel, then the B and C runs it started
5. [05 · The email](https://drive.google.com/file/d/1H2QCKUeNslkLglgvnKiiLwyzZR7It7EB/view?usp=sharing): the message received in the test inbox

## The brief

| Brief ([docs/ASSIGNMENT.md](docs/ASSIGNMENT.md)) | Where it lives |
|---|---|
| Scalable, event-driven CSV ingestion into RDS | presigned S3 POST → S3 event → ingest Lambda ([§1](#1-architecture), [§4](#4-ingestion)) |
| n8n in Docker; workflows A, B and C | [docker-compose.yml](docker-compose.yml), [n8n/workflows/](n8n/workflows/), described in [§3](#3-workflow-design) |
| Optimisation treasure hunt | [§2](#2-the-optimisation-treasure-hunt), with the measurements in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) |
| Emails through the SES node, only to test aliases | Workflow C, capped at 2 per run, never to a CSV address |
| Minimal upload UI | [ui/](ui/), a static Next.js page that follows each batch to the end |
| `serverless.yml`, setup, credentials | [infra/serverless.yml](infra/serverless.yml), [§5](#5-setup-and-deployment) |

## 1. Architecture

The diagrams in this README are Mermaid. On GitHub, hover over one for zoom, pan and
full-screen controls.

```mermaid
flowchart TD
    U(["Browser · upload page"])
    subgraph AWS["AWS · infra/serverless.yml"]
        P["presign λ"]
        S3[("S3 bucket")]
        I["ingest λ"]
        N["notify λ"]
        ST["status λ"]
        DB[("RDS Postgres")]
        SES["SES"]
    end
    subgraph N8N["n8n · docker-compose.yml"]
        GW["tunnel + nginx<br/>one webhook path"]
        B["B · matching"]
        C["C · notification"]
        A["A · crawler, daily"]
        G["LLM gateway"]
    end
    subgraph EXT["outside services"]
        WEB(["lender pages"])
        GEM(["Gemini"])
    end
    U -->|"1 · presign"| P
    U -->|"2 · upload"| S3
    S3 -->|"3 · event"| I
    I --> DB
    I -->|"4 · onSuccess"| N
    N -->|"5 · signed webhook"| GW
    GW --> B
    B -->|"6"| C
    C -->|"7 · email"| SES
    U -.->|"poll"| ST
    ST --> DB
    B --> DB
    A --> DB
    A --> G
    B --> G
    A --> WEB
    G --> GEM
```

<details>
<summary>Every resource and connection, in one detailed diagram (also as <a href="docs/architecture.png">a static image</a>)</summary>

```mermaid
flowchart TB
  user(["Browser · upload page (Next.js)"]):::ext

  subgraph AWS["AWS (Sydney)"]
    presign["presign λ · Function URL<br/>one-time POST policy"]
    s3[("S3 bucket<br/>uploads/ · rejects/")]
    notify["notify λ<br/>signs an HS256 JWT"]
    ssm[["SSM SecureString<br/>tunnel URL · webhook secret"]]
    dlq[["SQS dead-letter queue"]]
    subgraph VPC["VPC · no NAT"]
      ingest["ingest λ<br/>COPY + one SQL statement"]
      status["status λ · Function URL"]
      rds[("RDS Postgres 17 · db.t4g.micro<br/>users · loan_products · user_matches · notifications")]
    end
    ses["SES · sandbox"]
  end

  subgraph COMPOSE["Docker Compose · self-hosted"]
    cf["cloudflared · quick tunnel"]
    gw["webhook-gw (nginx)<br/>only POST /webhook/loan-match"]
    subgraph N8N["n8n 2.43.6 + task runner"]
      B["B · user-loan matching<br/>webhook · sweeper · manual"]
      C["C · notification"]
      A["A · product discovery<br/>daily 06:00"]
      W["Warm match cache"]
      G["LLM gateway"]
    end
  end

  subgraph EXT["Outside services"]
    sites(["Lender pages<br/>BankBazaar · HDFC · Kotak"]):::ext
    gemini(["Gemini API"]):::ext
    inbox(["Test inboxes<br/>+1 / +2 aliases"]):::ext
  end

  user -- "1 · presign" --> presign
  user -- "2 · POST file, gzip" --> s3
  user -. "5 · poll status" .-> status
  s3 -- "3 · ObjectCreated" --> ingest
  ingest -- "IAM token, TLS" --> rds
  status --> rds
  ingest -- "onSuccess" --> notify
  ingest -. "onFailure" .-> dlq
  notify --> ssm
  notify -- "4 · signed webhook" --> cf --> gw --> B
  B -- "6 · then" --> C
  N8N == "set-based SQL as n8n_app, verified TLS" ==> rds
  C -- "SendEmail, ≤ 2 per run" --> ses --> inbox
  A --> W
  A --> G
  W --> G
  B --> G
  A -- "robots.txt, fetch" --> sites
  G --> gemini

  classDef ext fill:#f6f8fd,stroke:#c9d2ec,color:#0b1533
```

</details>

What happens to one upload:

```mermaid
%%{init: {"sequence": {"actorMargin": 12, "width": 96, "boxMargin": 6, "noteMargin": 6, "messageMargin": 30}}}%%
sequenceDiagram
    autonumber
    actor U as Browser
    participant P as presign λ
    participant S3
    participant I as ingest λ
    participant DB as RDS
    participant N as notify λ
    participant W as n8n
    participant SES
    U->>P: name, size
    P-->>U: POST policy
    U->>S3: file (gzip)
    S3-)I: ObjectCreated
    I->>DB: COPY + 1 SQL
    I-)N: batch counts
    N->>W: signed webhook
    W-->>N: 202
    W->>DB: B: claim, write
    W->>DB: C: plan emails
    W->>SES: ≤ 2 emails
    Note over U,DB: the page polls the status λ (one query per poll)
```

1. The page asks the **presign** Lambda for a one-time S3 POST policy. The policy fixes the key,
   the content type and a 50 MB limit, and it expires in 5 minutes. The browser gzips the CSV and
   posts it straight to S3, so the file never goes through API Gateway (10 MB limit) or a Lambda
   payload (6 MB).
2. The S3 event invokes **ingest** inside the VPC. It streams the object into a Postgres `COPY`,
   and one SQL statement then validates, de-duplicates and upserts every row in a single
   transaction.
3. A Lambda Destination passes ingest's result to **notify**, or to an SQS dead-letter queue if
   ingest failed. Notify signs a JWT and posts it through a Cloudflare tunnel to an nginx gateway
   that forwards a single path, `POST /webhook/loan-match`, to n8n.
4. **Workflow B** checks the token, replies 202, matches the batch with two SQL statements and
   calls **Workflow C**. C records every notification and sends up to two emails through SES.
5. The page polls the **status** Lambda. One query returns the batch's timeline, the counts for
   each stage and the funnel shown on the page.

A few choices worth explaining:

- **IAM database auth for the Lambdas inside the VPC.** There is no NAT gateway, so these
  Lambdas can't reach SSM or Secrets Manager. An RDS IAM token is a SigV4 signature computed
  offline, so no password is stored anywhere. n8n connects as a separate role, `n8n_app`, which
  has no DELETE rights.
- **Lambda Destinations rather than an S3 "done" marker.** The Lambda service delivers them
  even from a VPC without NAT, they hand the row counts to notify, and failures end up in the
  DLQ.
- **Faster cold starts on the page's path.** presign and status sign requests with about 150
  lines of standard-library SigV4, pinned to botocore's output by tests, instead of importing
  boto3. That saves 120–250 ms per cold start.
- **The webhook is the only thing exposed.** It is an HS256 JWT checked by the n8n Webhook node,
  so the secret stays in an encrypted credential. A quick tunnel pointed straight at n8n would
  have published the editor, so the gateway forwards only that one path and the editor listens
  on localhost.
- **Settings live in an `app_config` table, not `$env`.** This keeps n8n's
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` default in place.
- **A sweeper on Workflow B** matches any batch whose webhook was lost, within 10 minutes.

The AWS account is limited to ap-southeast-2 (Sydney) by its sign-up type, while n8n runs on a
laptop in Bengaluru. So the workflows are written to need few database round trips: B makes 2
per batch, C makes 1 and the warm step 1. Before this they made 8 in total. The region is set in
one place, `AWS_REGION` ([DECISIONS AD27](docs/DECISIONS.md)).

## 2. The optimisation treasure hunt

The whole funnel for the 10,000-row sample, then each stage in detail:

```mermaid
flowchart TD
    S0["<b>S0 · every pair</b><br/>10,000 users × 24 products = 2,40,000"] -->|"one SQL join on the stated thresholds"| S2["<b>S2 · pass the hard rules</b><br/>61,937 pairs"]
    S2 -->|"only products with a fuzzy criterion"| R["<b>pending questions</b><br/>1,840"]
    R -->|"collapse to criterion + band"| S3["<b>S3 · cohorts</b><br/>1"]
    S3 --> S4{"<b>S4</b> · cached?"}
    S4 -->|"yes"| Z["<b>0 LLM calls</b> at upload time"]
    S4 -->|"no: a new wording"| S5["<b>S5</b> · up to 25 cohorts per request<br/>+ one rule compile · 2 calls, once"]
    S5 --> K[("llm_decisions · criterion_rules")]
    K --> Z
```

### The cost of the naive approach

Each user $u$ has facts $x_u$ = (income, credit score, age, employment). Each of the $M$ active
products $p$ has

- hard rules $H_p$: stated thresholds, plus conditional rules the crawler could compile, such as
  "salaried applicants need ₹30,000";
- fuzzy criteria $F_p$ that need judgement, such as "employees of private limited companies".
  For each one, the extraction records which facts it depends on, $D_f$.

Asking an LLM about every pair costs

$$C_0 = N \cdot M \quad\text{calls,}$$

which is 2,40,000 calls for this sample. At the free tier's 10 requests a minute that is about 17
days for a single upload, and it grows linearly with users.

### The pipeline

**S2: hard filter.** One set-based SQL join inside Postgres keeps only the pairs that pass the
stated rules:

$$Q = \{(u,p) : x_u \in H_p\}$$

A missing (NULL) threshold never filters anyone out, so recall comes first. Because this runs in
the database, n8n never holds users × products. At 100k users that would mean moving 15 MB of
users in and 2.4M items out.

**Pending questions.** Only pairs whose product has a fuzzy criterion need judgement:

$$R = \{(u,p,f) : (u,p) \in Q,\ f \in F_p\}$$

S3 on the sample data:

```mermaid
flowchart TD
    subgraph P["1,840 pending questions"]
        a["user 1: ₹82k, 760, Salaried, 29"]
        b["user 2: ₹1.1L, 790, Salaried, 41"]
        d["… 1,838 more"]
    end
    P -->|"HDFC's employer rule depends on employment only;<br/>income, score and age are inside HDFC's own limits"| K["<b>one cohort</b><br/>HDFC employer rule + Salaried"]
    K --> Q["one question to the LLM,<br/>cached for every later batch"]
```

**S3: cohorts.** A fuzzy criterion only sees the facts it depends on, so each question maps to a
cohort:

$$c(u,p,f) = \big(\kappa(f),\ \beta_p(x_u|_{D_f})\big)$$

Here $\kappa(f)$ hashes the criterion's wording and dependencies, so products that use the same
wording share verdicts. $\beta_p$ bands each numeric fact against the product's own thresholds:
with *threshold banding* a number becomes just "inside the product's range", while employment
stays exact. The number of cohorts therefore depends only on the products:

$$|C| \le \sum_{f} \prod_{d \in D_f} B_d,\qquad B_{\text{employment}} \le 3,\ B_{\text{numeric}} = 1$$

$N$ does not appear in the bound. As users arrive, the observed cohorts fill it like a coupon
collector, $E[\text{distinct after } n] = \sum_c \big(1-(1-\pi_c)^n\big)$, which levels off. On the
synthetic stress-test products it is 19 cohorts at 1k, 10k and 100k users alike.

**S4: cache.** Each verdict is stored once per $(\kappa, \text{band})$ in `llm_decisions` and
reused from then on.

S5's two judges, and how their votes combine:

```mermaid
flowchart TD
    P["a pair that needs judgement<br/>on fuzzy criterion f"] --> CJ["<b>cohort judge</b><br/>cached verdict c for the cohort"]
    P --> RJ["<b>rule judge</b><br/>compiled clauses on the exact facts → r"]
    CJ --> V{"vote"}
    RJ --> V
    V -->|"no rule, or r = c"| A["v = c"]
    V -->|"c = uncertain and r = ineligible"| B["v = ineligible<br/>(the rule vetoes)"]
    V -->|"any other disagreement"| U["v = uncertain<br/>kept as 'may qualify'"]
```

**S5: batched calls, two judges.** Each request carries up to $K = 25$ cohorts. A second judge
compiles each criterion once into clauses over the exact facts. For a criterion with cohort
verdict $c$ and rule verdict $r$, the final verdict is

$$
v = \begin{cases}
c & r = \varnothing \ \text{or}\ r = c \\
\text{ineligible} & c = \text{uncertain},\ r = \text{ineligible} \quad\text{(veto)} \\
\text{uncertain} & \text{otherwise}
\end{cases}
$$

So the rule can veto an uncertain verdict but can never vouch for one, and any disagreement
keeps the pair as "may qualify". The calls a batch needs are

$$\text{calls} = \left\lceil \frac{U}{K} \right\rceil + \left\lceil \frac{V}{25} \right\rceil$$

where $U$ is the number of uncached cohorts and $V$ the number of criteria without a compiled
rule.

Warming, in time:

```mermaid
%%{init: {"sequence": {"actorMargin": 12, "width": 96, "boxMargin": 6, "noteMargin": 6, "messageMargin": 30}}}%%
sequenceDiagram
    participant A as Workflow A
    participant W as Warm step
    participant G as LLM gateway
    participant DB as Postgres
    participant B as Workflow B
    A->>DB: upsert changed products
    A->>W: call after the crawl
    W->>DB: unjudged cohorts?
    W->>G: ≤ 25 cohorts / request
    G-->>W: verdicts + rules
    W->>DB: store them
    Note over B,DB: later, every upload
    B->>DB: claim: 0 uncached
    B->>DB: write: 0 LLM calls
```

**Warming.** `cohort_space()` lists every cohort the active products could produce, from the
products alone. Workflow A ends by judging those through the warm sub-workflow, so by the time a
batch arrives $U = V = 0$ and it makes **0 calls**. B keeps its own LLM branch for anything the
warm step missed.

### The numbers

The sample file against the current catalogue, as the upload page reports them:

| Stage | What is left | Count |
|---|---|---|
| S0 naive | (user, product) pairs | 2,40,000 |
| S2 hard filter | pairs passing the stated rules | 61,937 |
| pending | pairs with a fuzzy criterion | 1,840 |
| S3 cohorts | distinct (criterion, band) | 1 |
| S5 LLM calls | at upload time / once per product change | **0** / 2 |

To pick the LLM stage, I scored 14 strategies against a per-pair oracle: one question per call,
with the exact facts, on 109 pairs. A false drop hides a product the customer might qualify for,
so zero false drops was the first requirement. These are the strategies that matter:

| Strategy | Calls (10k users) | False drops | Rejections caught |
|---|---|---|---|
| per pair (the oracle) | 50,489 | 0 | 14 / 14 |
| rules only, no LLM | 0 | 0 | 0 / 14 |
| threshold cohorts | 1 | 0 | 6 / 14 |
| **threshold cohorts + rule veto (chosen)** | **2** | **0** | **10 / 14** |
| coarse cohorts, 25 per call | 2 | 11 | 9 / 14 |
| one call per user | ≈ 10,000 | 6 | 13 / 14 |

Packing finer bands into one request led the model to draw lines between adjacent bands, which
dropped pairs it shouldn't have. Threshold cohorts only ask questions that can be answered for a
whole range. On a held-out sample, the oracle confirmed every one of the 40 pairs the chosen
strategy dropped, and the strategy caught an estimated 92% of what per-pair judging rejects.

It scales flat. These runs used `make bench` on synthetic products with 15 fuzzy criteria, since
the real pages yield only one:

| Users | Naive calls | After S2 | Pending | Cohorts | Calls cold | Calls warmed | Recall |
|---|---|---|---|---|---|---|---|
| 1,000 | 25,000 | 11,601 | 5,064 | 19 | 2 | 0 | 1.000 |
| 10,000 | 2,50,000 | 1,15,896 | 50,489 | 19 | 2 | 0 | 1.000 |
| 1,00,000 | 25,00,000 | 11,56,519 | 5,03,545 | 19 | 2 | 0 | 1.000 |

Once the LLM was off the critical path, writing the matches became the cost. Four changes fixed
that:

- **Statement-level reference checks** instead of row-level foreign keys: 51 s became 12 s at
  100k users.
- **Plain INSERTs for new users**, so only returning users go through an upsert.
- **A BRIN index on batch.**
- **One row per user** (`user_matches`) instead of one per pair, with `matches` kept as a view.
  The write fell from 1.16 to 0.37 s at 10k users and from 25.5 to 8.3 s at 100k, with 6–8× less
  WAL.

Through n8n a 10k batch is matched about 1 s after the webhook. On the deployed stack, from India
to Sydney, a 10k upload takes about 13 s from drop to done.

These trade-offs were accepted on purpose:

- A cohort verdict applies to everyone in that cohort.
- Cached verdicts last until a criterion's wording changes.
- Roughly 8% of what a per-pair LLM would reject goes out as "may qualify" instead.

Recall against the stated rules is never traded. All 14 strategies, the method and the
trade-offs are in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

I also considered and dropped these alternatives:

- **Embeddings.** The facts are structured numbers, so there is nothing semantic to retrieve.
- **A classifier trained on LLM labels.** It would only approximate rules that can be written
  down exactly.
- **Matching in a Code-node loop.** It is exact, but its working set grows with N × M, while
  the SQL keeps the data in the database.

## 3. Workflow design

The workflows are built from templates in [n8n/src/](n8n/src/). Their SQL lives in [sql/](sql/)
and their Code-node JavaScript in [n8n/code/](n8n/code/), both injected at build time, so the
same code runs in n8n, the tests and the bench. Every canvas has sticky notes. A few rules apply
throughout:

- A SQL node does a whole step in one round trip.
- No node ever holds users or pairs, only cohorts, LLM requests or emails.
- Every workflow is safe to re-run.

```mermaid
flowchart TD
    T1(["Daily at 06:00"]) --> CFG["Load app_config"]
    T2(["Run manually"]) --> CFG
    CFG --> SRC["Load sources"] --> LOOP{{"Loop over sources"}}
    LOOP -->|"next source"| RUN["Start crawl run"] --> ROB["Fetch robots.txt"] --> PLAN["Plan page fetches<br/>robots.txt check in code"]
    PLAN -->|"allowed"| FETCH["Fetch pages<br/>honest User-Agent"] --> COL["Collect pages<br/>every page 200?"]
    COL --> CLEAN["Clean page text<br/>table rows kept as cell · cell"] --> HASH["Hash page text<br/>SHA-256"] --> CH{"Page changed?"}
    CH -->|"no"| UNCH["Mark unchanged<br/><b>0 LLM calls</b>"]
    CH -->|"yes"| EXT["Extract with LLM<br/>gateway, strict JSON schema"] --> VAL["Validate products<br/>code decides what filters"] --> UPS["Upsert products<br/>+ criteria history diff"]
    UNCH --> FIN["Finish crawl run"]
    UPS --> FIN
    PLAN -.->|"disallowed or error"| FIN
    COL -.->|"a page not 200"| FIN
    EXT -.->|"LLM failed"| FIN
    VAL -.->|"validation error"| FIN
    FIN --> PAUSE["Polite pause<br/>live mode only"] --> LOOP
    LOOP -->|"all sources done"| STALE["Deactivate stale products<br/>unseen for 7 days"] --> SUM["Crawl summary"] --> WARM["Warm match cache"]
```

**A · Loan product discovery** runs daily at 06:00 and loops over the sources:

1. Read robots.txt and check each URL against it.
2. Fetch the pages with an honest User-Agent.
3. Clean them into text that keeps table rows as `cell | cell`. BankBazaar's 711 KB of HTML
   becomes 50 KB.
4. Hash the text with SHA-256. **If nothing changed, skip the LLM.**
5. Otherwise extract through the LLM gateway, validate in code, upsert with a criteria-history
   diff, and close the crawl run.

After the loop it retires products not seen for 7 days, writes a summary and calls the warm
step. Every Code node has an error output, so a failing source closes its own run as `error`
and the loop moves on.

How the crawl works:

- **Sources.** BankBazaar (an aggregator with rate and eligibility tables for 20+ lenders),
  HDFC Bank and Kotak, with Hero FinCorp as a backup. I picked pages whose criteria are in the
  server-rendered HTML. Eight other sites were rejected for bot walls, moved pages or
  JavaScript-only data ([DECISIONS](docs/DECISIONS.md#crawl-sources)).
- **Extraction.** The LLM reports what the page says, with a confidence per field, and sorts
  leftover criteria into `compilable`, `fuzzy` or `unknown_field`. Code then decides what
  actually filters: it converts annual income to monthly, uses the most permissive income tier,
  reads stated score minimums, and keeps low-confidence fields out of the filter.
- **Product identity.** Products are keyed by lender and normalised name, so a reworded
  extraction updates a product instead of duplicating it.
- **Fixtures.** `CRAWL_MODE=fixtures` (the default) serves saved copies of the real pages
  through the same HTTP nodes, so tests and demos don't depend on live sites. `live` crawls
  the real pages.

```mermaid
flowchart TD
    WH(["Signed webhook<br/>JWT checked, 202 at once"]) --> TOK["Read token claims<br/>trust the claims, never the body"] --> LOOP
    SW(["Sweeper · every 10 min"]) --> FIND["Find waiting batches"]
    MAN(["Run manually"]) --> FIND
    FIND --> ANY{"Anything waiting?"}
    ANY -->|"no"| NONE["Nothing waiting"]
    ANY -->|"yes"| LOOP{{"Loop over batches"}}
    LOOP -->|"next batch"| CLAIM["<b>Claim batch</b> · 1 SQL statement<br/>claim + config + uncached cohorts"]
    CLAIM --> OK{"Claimed?"}
    OK -->|"no, already taken"| NEXT["Next batch"]
    OK -->|"yes"| LLM{"LLM needed?"}
    LLM -->|"no: warm cache"| WRITE
    LLM -->|"yes"| BUILD["Build cohort requests<br/>up to 25 per request"] --> JUDGE["Judge cohorts<br/>LLM gateway"] --> STORE["Collect + store verdicts"] --> WRITE
    WRITE["<b>Write matches</b> · 1 SQL statement<br/>every pair once, with its status + metrics"] --> NOTIFY["Notify users<br/>Workflow C"] --> NEXT
    NEXT --> LOOP
    CLAIM -.->|"error"| FAIL["Mark batch failed<br/>reason shown on the page"]
    WRITE -.->|"error"| FAIL
    FAIL --> NEXT
```

**B · User-loan matching** has three triggers that share one path: the signed webhook, a
sweeper every 10 minutes and a manual run.

1. Read token claims. Only the verified claims are trusted, never the request body.
2. Loop over the batches.
3. **Claim batch.** One statement claims the batch idempotently, reads the config and returns
   only the uncached cohorts.
4. If any are uncached: build cohort requests → judge them through the gateway → store the
   verdicts.
5. **Write matches.** One statement inserts every pair once, with its final status and the
   metrics for each stage.
6. Call C.

A failure marks the batch failed with its reason, and the page shows that reason.

```mermaid
flowchart TD
    T(["Called by Workflow B"]) --> PLAN
    M(["Run manually"]) --> PLAN
    PLAN["<b>Plan notifications</b> · 1 SQL statement<br/>refuse unsafe settings · record every user ·<br/>build at most EMAIL_CAP emails · never reads users.email"] --> CHK["Check email settings<br/>same rules again, in code"]
    CHK --> REN["Render emails<br/>HTML + text, escaped"] --> SEND{"Send?"}
    SEND -->|"suppressed, or nothing new"| DONE["Done"]
    SEND -->|"test_aliases"| REC["Check recipients<br/>alias · used once · cap · sender"] --> LOOP{{"Loop over emails"}}
    LOOP -->|"next email"| SES["Send email<br/>AWS SES node, send-only key"] --> RES["Read SES result"] --> RECD["Record result"] --> PACE["Pace sends<br/>SES rate"] --> LOOP
    LOOP -->|"all sent"| DONE
```

**C · User notification**:

1. **Plan notifications.** One statement refuses to do anything unless the email settings are
   safe. It then records a notification row for every user with new offers: the first
   `EMAIL_CAP` get a test alias as recipient and everyone else is marked `suppressed`. It never
   selects `users.email`.
2. Render the emails, HTML and text, escaped.
3. Check the recipients: each must be an alias, used once, and from the configured sender.
4. Send through the AWS SES node one at a time, recording each result and pausing to stay under
   the SES rate.

A rerun sends nothing.

```mermaid
flowchart TD
    IN(["requests from A, B or the warm step"]) --> CFG["Load app_config + today's usage"] --> BLD["Build provider request"] --> BUD{"Within daily budget?"}
    BUD -->|"no"| RET["Return result<br/>the same shape for every request"]
    BUD -->|"yes"| CALL["Call provider<br/>llm_concurrency at a time, paced under llm_rpm"] --> PARSE["Parse response"] --> LOG["Log LLM call"] --> RST["Restore response"] --> RTY{"Retry? 429 or 5xx"}
    RTY -->|"yes"| BACK["Back off<br/>RetryInfo, else exponential"] --> CALL
    RTY -->|"no"| RET
```

**LLM gateway** is a sub-workflow and the only place that calls a model.

- It returns the same shape for every request.
- It checks the daily budget before each call.
- It sends requests in parallel, paced under the RPM limit.
- It retries 429 and 5xx responses with backoff and logs every attempt.

```mermaid
flowchart TD
    A(["Called by A after each crawl"]) --> FIND["Find unjudged cohorts<br/>cohort_space() minus cache"] --> N{"LLM needed?"}
    N -->|"no"| NO["Nothing to judge<br/>one 6 ms query"]
    N -->|"yes"| BLD["Build cohort requests"] --> G["Judge cohorts · LLM gateway"] --> ST["Store verdicts + rules"]
```

**Warm match cache** is one query plus the same nodes B uses to judge cohorts.

## 4. Ingestion

A batch's life, as the status page reports it:

```mermaid
stateDiagram-v2
    [*] --> uploaded: file lands in S3
    uploaded --> ingested: ingest λ, COPY + 1 SQL statement
    uploaded --> failed: file can't be read (reason kept)
    ingested --> matching: B claims it (webhook or sweeper)
    matching --> matched: Write matches
    matched --> notifying: C has emails to send
    matched --> notified: nothing to send
    notifying --> notified: last email recorded
    matching --> failed: error (reason kept)
    notified --> [*]
    failed --> [*]
```

Inside the ingest Lambda:

```mermaid
flowchart TD
    EV(["S3 ObjectCreated"]) --> LOGIN["IAM login to RDS<br/>hedged, kept warm every 5 min"] --> DUP{"bucket + key + etag<br/>seen before?"}
    DUP -->|"yes: redelivery"| SKIP["do nothing<br/>notify skips it"]
    DUP -->|"no"| STREAM["Stream the object<br/>gunzip if the page compressed it"] --> HDR{"readable, with<br/>all 7 columns?"}
    HDR -->|"no"| FAILB["failed batch<br/>with a reason the page shows"]
    HDR -->|"yes"| COPY["COPY raw text into a temp table"] --> SQL["<b>One SQL statement</b><br/>validate every row · last user_id wins ·<br/>upsert users · close the batch"]
    SQL --> REJ["rejects/batch.csv<br/>line · reason · raw values"]
    SQL --> OUT["counts → notify λ<br/>via Lambda Destination"]
```

| Concern | Handling |
|---|---|
| Large files | The file goes straight to S3, gzipped. Ingest streams it into `COPY` at about 50k rows/s, so 100k rows take about 2 s |
| Validation | One SQL statement with ordered `CASE` checks, so a value is only cast once it's known to be castable. Checks: UUID id, name, email shape, income > 0, score 300–900, employment category, age 18–100 |
| Messy CSVs | Columns matched by name in any order; extra columns ignored; BOM and CRLF accepted; values trimmed |
| Bad rows | Each is skipped with a reason in `rejects/<batch>.csv` (line, reason, raw values). For a repeated `user_id` the last row wins |
| Unreadable files | Missing columns, a corrupt gzip, NUL bytes or a field over 128 KB: the whole file is refused with a reason, which the page shows |
| Duplicate S3 events | Idempotent on (bucket, key, etag) |
| Failures | Database logins are hedged and never cancelled (a fresh container's RDS IAM check can take seconds), and a 5-minute schedule keeps ingest and status logged in. Then come Lambda's async retries, then the DLQ. The sweeper catches a lost webhook ([AD41](docs/DECISIONS.md)) |

The rejection reasons are checked against an independent Python validator: on a 100k file with
2,036 injected faults, each reason's count matches the number of faults of that kind.

## 5. Setup and deployment

You need Docker, [uv](https://docs.astral.sh/uv/), Node 24 and make. For AWS you also need the
AWS CLI and a free Serverless Framework v4 login. The file avoids v4-only syntax, so
[osls](https://github.com/oss-serverless/osls) can deploy it too.

The local stack:

```mermaid
flowchart LR
    subgraph DC["docker compose · ports bound to 127.0.0.1"]
        N8N["n8n 2.43.6<br/>editor on :5678"] <--> RUN["n8n-runners<br/>Code nodes"]
        N8N --> NDB[("n8n-db<br/>workflows, executions")]
        N8N --> ADB[("app-db<br/>local Postgres :5433")]
        N8N --> FX["fixtures<br/>saved lender pages"]
        GW["webhook-gw · nginx<br/>only POST /webhook/loan-match"] --> N8N
        CF["cloudflared<br/>quick tunnel"] --> GW
    end
    NL(["notify λ on AWS"]) --> CF
    N8N -.->|"make use-rds"| RDS[("RDS Postgres")]
```

### Run locally

```bash
make setup         # Python env from uv.lock
make env           # .env from .env.example, with every local secret generated
                   # then set GEMINI_API_KEY in .env (free key from aistudio.google.com)
make bootstrap     # containers, migrations, app_config, credentials, workflows, publish (re-runnable)
make crawl         # Workflow A once, then the warm step
make ui-install && make ui      # upload page on http://localhost:8090
```

n8n is at http://localhost:5678. Create the owner account on your first visit; the workflows and
credentials are already imported and published.

### n8n credentials

`make bootstrap` builds these four from `.env` and imports them under fixed IDs, so the
workflows find them without any edits. n8n encrypts them on import, and none are in git.

| Credential | Type | Holds | Used by |
|---|---|---|---|
| App DB (n8n_app) | Postgres | Local: host `app-db`, db `loans`, user `n8n_app`, `N8N_APP_DB_PASSWORD`. RDS: `RDS_HOST`, `RDS_N8N_APP_PASSWORD`, SSL `require` | every SQL node |
| AWS SES (send-only IAM user) | AWS | `AWS_REGION`, `SES_AWS_ACCESS_KEY_ID`, `SES_AWS_SECRET_ACCESS_KEY` | C's SES node |
| Webhook JWT (HS256) | JWT Auth | passphrase `WEBHOOK_HMAC_SECRET`, algorithm HS256 | B's webhook |
| Gemini API key | Header Auth | `x-goog-api-key: GEMINI_API_KEY` | LLM gateway |

To create them by hand instead, go to **Credentials → New** in n8n and enter the same values.
For RDS, leave "Ignore SSL issues" off: compose mounts the RDS CA bundle and sets
`NODE_EXTRA_CA_CERTS`, so the certificate is verified. Bootstrap refuses to import an AWS key
that also appears in `~/.aws/credentials`, which keeps admin keys out of n8n.
`make use-rds` and `make use-local` switch the database credential and `app_config` between RDS
and the local database.

### SES

Verify the sender and both aliases as identities in SES (ap-southeast-2), for example
`you@gmail.com`, `you+1@gmail.com` and `you+2@gmail.com`. The SES sandbox only delivers to
verified addresses. Then create an IAM user for n8n with only this policy and put its keys in
`SES_AWS_*`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ses:SendEmail", "ses:SendRawEmail"],
    "Resource": [
      "arn:aws:ses:ap-southeast-2:<account-id>:identity/you@gmail.com",
      "arn:aws:ses:ap-southeast-2:<account-id>:identity/you+1@gmail.com",
      "arn:aws:ses:ap-southeast-2:<account-id>:identity/you+2@gmail.com"
    ]
  }]
}
```

Set `SES_SENDER`, `EMAIL_ALIASES` and `EMAIL_CAP=2` in `.env`. Set `EMAIL_MODE=test_aliases`
to send, or `suppressed` (the default) to record and preview without sending. Gmail may file
these emails as spam, because gmail.com's DMARC policy is `p=none`.

### Deploy to AWS

```bash
echo "MY_IP_CIDR=$(curl -s https://checkip.amazonaws.com)/32" >> .env   # the only IP RDS accepts besides the Lambdas
make tunnel          # Cloudflare quick tunnel to webhook-gw
make aws-params      # SSM: RDS master password, webhook secret, tunnel URL
make deploy          # VPC, bucket, 4 Lambdas, DLQ, RDS (about 12 min)
make migrate-rds && make catalog-to-rds && make rds-ca
make use-rds         # point n8n at RDS
UI_TARGET=aws make ui
```

`make deploy` ends by logging the Lambdas in to RDS (`make warm`), so the first upload is fast.
`make aws-upload` sends `data/warmup.csv` through the whole AWS path, timed, without sending
any email. After the laptop sleeps or the network changes, the tunnel needs restarting:
`make tunnel && make tunnel-ssm`. Stop RDS
between sessions with `make rds-stop`. `make destroy` removes everything.

### Seeing every branch of every workflow

Each workflow is built to skip work it has already done. A second run of A finds the pages
unchanged and skips the LLM. B's manual run stops at "Nothing waiting" when no batch is waiting.
C finds the batch already notified. `make demo-reset` undoes exactly those skips, and it never
deletes users, products, matches or notifications:

```bash
make demo-reset                              # A re-extracts hdfc-bank; the warm cache is cleared
make demo-reset ARGS="--cold-batch 1000"     # a waiting batch with nothing cached, for B's "Run manually"
make demo-reset ARGS="--target rds"          # the same against RDS
```

## 6. Testing and repository

`make check` runs ruff, mypy `--strict`, **185 Python tests** and **73 JavaScript tests**, and
fails if the generated workflows or the data profile are out of date. The Python tests run
against throwaway databases and cover:

- ingest, checked against an independent validator;
- every Lambda handler, under moto;
- SigV4 output equal to botocore's;
- the matching and notification SQL, run as `n8n_app`;
- workflow shape rules;
- the cost guards on `serverless.yml`.

`make match-e2e` compares n8n's matches with brute force and reports recall and precision of
1.0000. The [docs/TESTING.md](docs/TESTING.md) guide lists manual checks with their expected
output.

```
infra/serverless.yml   AWS stack          services/   the four Lambdas + shared code
docker-compose.yml     n8n stack          sql/        migrations and workflow SQL
n8n/src, n8n/code      workflow sources   ui/         upload page
n8n/workflows          importable JSON    bench/      optimisation bench and results
```

For more depth, [docs/DECISIONS.md](docs/DECISIONS.md) explains every design decision and its
measurement, and [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) holds the treasure-hunt data.

**Cost and limits.** Running this costs about $0. The only billed item is RDS's public IPv4
address (about $0.005 an hour) while RDS runs. Gemini stays on its free tier: an unchanged crawl
costs nothing, and so does an upload.

What I would do next:

- Run n8n in the same region as RDS, behind a named tunnel or a private link.
- Add the prompt version to the cache key, so that a model upgrade re-judges each cohort once.
- Spot-check a few users per cohort during the crawl.
