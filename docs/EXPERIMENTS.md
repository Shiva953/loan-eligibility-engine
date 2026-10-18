# Experiments: the optimisation treasure hunt (Workflow B)

The brief: an LLM judging every (user, product) pair would be "extremely slow and expensive", so
reduce the pairs before any LLM sees them. This document records every approach that was tried
or considered, how each was measured, and why the final design won. Everything here is
reproducible:

```bash
make bench                         # the funnel and timings at 1k/10k/100k users (no LLM calls)
make bench-llm                     # LLM strategies vs the oracle (cached: 0 calls on a rerun)
make bench-llm ARGS=--holdout      # the chosen strategy on a fresh, held-out sample
make match-e2e ARGS="--n 10000"    # the same pipeline through n8n, checked vs brute force
```

## 1. Result in one table

Real data: `data/users.csv` (10,000 users) against the 32 products Workflow A crawled.

| Stage | What remains | Count |
|---|---|---|
| S0 naive | every (user, product) pair goes to the LLM | **320,000** LLM calls |
| S2 hard filter (one SQL join) | pairs that pass the stated rules | 236,482 pairs, no LLM |
| needs judgement | pairs whose product has a fuzzy criterion | 3,389 pairs |
| S3 cohorts | distinct (criterion, banded facts) | 1 cohort |
| S4 cache + S5 batching | LLM calls (cohort judge + rule judge), made by the warm step right after the crawl | **2** calls per product change |
| every batch, including the first | everything is cached before it arrives | **0** calls at match time |

Recall and precision against a brute-force evaluator of every pair: **1.000 / 1.000**, at every
size from 1k to 100k users, on real and synthetic products (section 7). Through n8n, a 10k batch
goes from webhook to `matched` in **2.1–2.7 s** once its cohorts are cached (about 8.5 s on the
same path before this phase; section 6).

**The current catalogue (2026-10-18).** Since DECISIONS AD37, the crawl yields 24 matchable
products: stated score minimums filter, and products without criteria match nobody. The same
file through the upload page gives 2,40,000 naive pairs → **61,937** past the hard filter →
**1,840** needing judgement → **1** cohort → **0** calls at match time. The table above was
measured on the earlier 32-product catalogue. The method and the call counts are unchanged.

The real pages yield only one fuzzy criterion, so the LLM stage was stress-tested on a synthetic
product set (`bench/fixtures/synthetic_products.json`: 25 products, 15 fuzzy criterion instances
over 12 distinct wordings): 250,000 naive pairs for 10k users, 50,489 pending (pair, criterion)
questions, still **2 LLM calls**, made right after the crawl so that batches make none
(section 8), with zero false drops against a per-pair oracle and an estimated 92% of the
oracle's rejections caught (section 4).

## 2. The problem as numbers

Users *u* (N of them) have facts x_u = (income, credit score, age, employment). Each of the M
active products *p* has:

- **hard rules** H_p: thresholds that are either stated (minimum income, score range, age range,
  allowed employment) or compilable rules ("salaried applicants need ₹30,000"). NULL never
  filters (recall first).
- **fuzzy criteria** F_p ("high-income individuals", "young professionals at the start of their
  career"). Workflow A's extraction records for each criterion the facts it depends on, D_f.

The naive approach asks the LLM about every pair: **C₀ = N·M** calls. The pipeline:

1. **S2, hard filter.** Q = {(u, p) : x_u ∈ H_p}, one set-based SQL join. No LLM.
2. **Pending questions.** R = {(u, p, f) : (u, p) ∈ Q, f ∈ F_p}. Only these need judgement.
3. **S3, cohorts.** A fuzzy criterion is judged only on the facts it depends on, so (u, p, f)
   maps to a cohort c = (key(f), β_p(x_u restricted to D_f)): key(f) hashes the wording and
   D_f (so products that share a criterion share its verdicts), and β_p bands each numeric fact.
   With **threshold banding**, a numeric fact collapses to the product's own allowed range
   (one band); employment stays exact. The number of cohorts is bounded by the products alone:

   |cohorts| ≤ Σ over criteria f of ∏ over d ∈ D_f of B_d,  with B_employment ≤ 3 and
   B_numeric = 1 (threshold), 4 (coarse), ≈10–21 (fine), or every distinct value (exact).

   The bound does not contain N. As users arrive, observed cohorts fill the bound like a
   coupon collector: with cell probabilities π_c among the users that pass a product, the
   expected number of distinct cohorts after n users is Σ_c (1 − (1 − π_c)ⁿ), which saturates.
4. **S4, cache.** Verdicts are stored per (key, signature) forever, so over the system's life
   the LLM answers each cohort once, whatever the number of batches.
5. **S5, batching.** K cohorts per call; plus one call per 25 criteria for the rule judge
   (section 4): calls = ⌈uncached cohorts / K⌉ + ⌈uncompiled criteria / 25⌉. A rerun: 0.

Saturation, measured (cohorts per banding mode on the synthetic products; `make bench`):

| Users | threshold | coarse | fine | exact |
|---|---|---|---|---|
| bound from the products alone | 19 | 47 | 211 | 3,015 |
| real profile, 1k / 10k / 100k | 19 / 19 / 19 | 43 / 43 / 43 | 120 / 123 / 123 | 760 / 930 / 941 |
| wide profile, 1k / 10k / 100k | 19 / 19 / 19 | 46 / 47 / 47 | 162 / 205 / 209 | 779 / 2,154 / 2,869 |

The real CSV is narrow (income ₹25k–150k, score 650–800, age 22–50), so it never reaches the
coarse and fine bounds; the wide profile does. Threshold cohorts sit at their bound from the first
1,000 users, which is also why judging every possible cohort ahead of time costs nothing extra
(section 8).

## 3. Stage S2: the deterministic filter

Four exact implementations, timed on the same users and products (`make bench`; JavaScript
times exclude moving the data into the process, which is the real cost in n8n):

| Variant | Idea | 10k users | 100k users |
|---|---|---|---|
| d0 JavaScript nested loop | what "match in a Code node" becomes: N × M tests, one item per pair | 35 ms, +36 MB heap | 265 ms, +351 MB heap |
| d4 JavaScript bitmask | cut points per field, one bitmask of products per interval, AND of 3 lookups | 11 ms, +19 MB heap | 89 ms, +169 MB heap |
| **d1 SQL join (production)** | `match_candidates()`: one join plus one anti-join for conditional rules | 43 ms | 422 ms |
| d2 SQL decision cells | each occupied cell of the cut-point grid tested once, then joined back | 57 ms | 545 ms |

All four reproduce the brute-force reference exactly. Evaluation is never the bottleneck: the
output is (2.37M pairs at 100k users). The SQL join wins because nothing leaves the database:
the alternative ships 15 MB of users into n8n and 2.37M items back out. n8n therefore holds one
row of counts and at most a handful of LLM requests, never users × products (DECISIONS AD17).

## 4. Stages S3–S5: the LLM stage

### Method

- **Oracle:** one question per sampled (user, product, criterion), giving all four facts
  exactly, K = 1. It is the most faithful and most expensive use of the LLM. Each question goes
  through the production prompt builder (a cohort of one user), so disagreements come from the
  strategy, not from wording. 109 pairs over 55 users, every criterion instance covered.
- **Noise floor:** 15 oracle questions asked again: 15/15 identical.
- **Errors are not symmetric.** A **false drop** (oracle keeps the pair, the strategy rejects
  it) hides a product the user may qualify for. A **false keep** adds a "may qualify" line the
  oracle would not. So the first requirement is zero false drops; then fewer calls; then
  *drop recall*, the share of the oracle's rejections the strategy also makes (how much
  filtering the LLM stage delivers at all).
- Model `gemini-3.5-flash-lite`, temperature 0, structured output. 14 strategies, 322 requests
  in total (369 attempts with retries), all cached in `bench/llm_cache/`: a rerun of the whole
  analysis costs 0 calls.

### Results (synthetic products, 10k users, 50,489 pending questions)

| Strategy | LLM calls for the batch | False drops (of 95 oracle keeps) | False keeps (of 14 oracle rejections) | Drop recall | Decision agreement | 3-way agreement |
|---|---|---|---|---|---|---|
| S0 per pair (the oracle itself) | 50,489 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| rules only (no LLM) | 0 | 0 | 14 | 0.000 | 0.872 | 0.459 |
| threshold cohorts, K = 25 | 1 | **0** | 8 | 0.429 | 0.927 | 0.670 |
| compiled rule with abstain region | 1 | **0** | 10 | 0.286 | 0.908 | 0.450 |
| **threshold cohorts + rule veto (chosen)** | **2** | **0** | **4** | **0.714** | **0.963** | **0.706** |
| coarse cohorts, K = 1 | 43 | **0** | 6 | 0.571 | 0.945 | 0.661 |
| coarse cohorts, K = 25 | 2 | 11 | 5 | 0.643 | 0.853 | 0.606 |
| coarse cohorts, K = 10 | 5 | 16 | 4 | 0.714 | 0.817 | 0.569 |
| coarse cohorts, K = 50 | 1 | 6 | 6 | 0.571 | 0.890 | 0.596 |
| coarse cohorts, stratified K = 25 | 10 | 4 | 7 | 0.500 | 0.899 | 0.688 |
| fine cohorts, K = 25 | 5 | 5 | 5 | 0.643 | 0.908 | 0.523 |
| exact cohorts, K = 25 | 38 | 4 | 1 | 0.929 | 0.954 | 0.734 |
| adaptive (coarse, then fine where uncertain) | 3 | 11 | 5 | 0.643 | 0.853 | 0.642 |
| one call per user | ≈ 10,000 | 6 | 1 | 0.917 | 0.894 | 0.773 |
| compiled rule, two-valued | 1 | 26 | 0 | 1.000 | 0.761 | 0.468 |

### What the numbers say

1. **Batching borderline bands breaks judgement.** The same 43 coarse cohorts have no false
   drops when asked one per call, and 6–16 when packed 10, 25 or 50 to a call. Listing adjacent
   bands of one criterion invites the model to draw a line between them. Putting at most one
   band of each criterion in a request ("stratified") cut false drops from 11 to 4 but not to 0,
   and 11 of the 43 cohorts flipped verdict between the two layouts: on borderline bands the
   model's commitment is not stable, whatever the packing.
2. **Threshold banding asks only questions the model can answer.** A threshold cohort is the
   product's whole allowed range, so the model commits only when the criterion is clear for the
   whole range (typically employment: "salaried applicants…" for a self-employed cohort) and
   otherwise says uncertain, which keeps the pair. Zero false drops in one call.
3. **Plain compilation invents cut-offs.** Asked to turn "high-income individuals" into
   conditions, the model writes "income ≥ ₹1,50,000" and rejects everyone below: 26 false
   drops. Adding an abstain region (clauses tagged eligible or ineligible, everything else
   uncertain, "do not invent cut-offs") removes them all: zero false drops, but it only rejects
   where the wording states a number ("minimum credit score stated as 720").
4. **The two safe judges see different things.** Cohorts resolve categorical dependencies
   exactly but blur numbers; compiled rules state numeric cut-offs that the wording gives. On the
   sample their rejections do not overlap (threshold: the "salaried…" criterion, rule: the "720"
   criterion), and they never contradict each other.
5. **So the rule may veto, never vouch.** Combining them so that a rule can turn an *uncertain*
   cohort verdict into *ineligible* (and any disagreement stays uncertain) keeps zero false
   drops and raises drop recall from 0.43 to 0.71. Letting the rule also turn uncertain into
   eligible ("vouch") catches the same rejections but removed 12 of the oracle's 45 legitimate
   "may not apply" caveats (3-way agreement 0.615 vs 0.706): compiled rules over-reach on what
   the facts can't show (for Kotak's "salaried … at MNCs" the model wrote "eligible if
   salaried" while noting that company type is unknown). Veto-only is the strict improvement.

### Held-out validation

The winner was picked from 14 strategies on one sample, so it was checked on fresh pairs
(`make bench-llm ARGS=--holdout`), stratified by its own decision: 40 pairs it drops and 60 it
keeps, none from the first sample.

| Stratum | Pairs | Oracle verdicts | Errors |
|---|---|---|---|
| dropped by the strategy | 40 | 40 ineligible | **0 false drops** (threshold alone missed 20, the rule alone missed the other 20) |
| kept by the strategy | 60 | 32 eligible, 27 uncertain, 1 ineligible | 1 missed rejection |

Weighted by the population (the strategy drops 8,143 of 50,380 pending questions, 16.2%): drop
precision 1.0 (rule of three: ≥ 0.925 at 95%), missed rejections in the keeps 1.7%, so an
estimated **92%** of the rejections a per-pair LLM would make, for 2 calls instead of 50,489.
The first sample's lower 0.71 comes from its design: it covers every criterion equally, so the
borderline criteria ("young professionals", "mid-career") weigh as much as the two criteria that
account for most real rejections.

### Batch size and latency

Free-tier Gemini latency is a fixed cost per request, not per token: one question (≈53 output
tokens) took a median 8–10 s, 25 questions (885 tokens) 7 s, 50 questions (1,902 tokens) 22 s.
K = 25 holds all 19 threshold cohorts in one call, so a first batch pays one cohort call and one
compile call, and every later batch pays none. Thinking tokens are small (they are counted in
`tokens_out`), so a lower thinking level would not help.

## 5. The write path

At 100k users the write is 2.37M rows and dominates everything else. Measured in rolled-back
transactions, on the same data:

| Change | Before | After |
|---|---|---|
| Row-level foreign keys (3) → one statement-level anti-join per key | 50.9 s | 12.0 s (with pre-sorted rows) |
| `ON CONFLICT` for every row → plain INSERT for new users, upsert for returning users | 3.13 s (10k), 37.7 s (100k) | 1.41 s (10k), 22.9 s (100k) |
| B-tree (batch_id, status) → BRIN (batch_id), 100k | WAL 960 MB, 39.4 s | WAL 766 MB, 24.4 s; per-batch count 0.35–0.58 s vs 0.92–1.37 s |
| Replay of an unchanged batch, 100k | 89 s, every row rewritten and `notified_at` reset | 10.4 s, nothing written, nobody re-notified |
| Sort spill (work_mem 4 MB → 64 MB for this statement), ranking rules materialised | 23 MB spilled to disk | in memory |

`ON CONFLICT … DO UPDATE … WHERE` turned out to lock every conflicting row before testing the
WHERE (V29), so unchanged rows are now filtered out by a read-only anti-join first.

The final statement, rule judge and replay filter included, was checked against the split
version without them in a controlled A/B: fresh database, real products, 10k users, 5 runs each,
interleaved, a checkpoint before every run. Final: median **1.12 s** (1.00–1.23); split version
without the extras: 1.33 s (1.02–2.15); the original upsert on the same data: about 3.1 s. The
bench in section 7 runs 100k writes back to back, so its timings carry WAL and checkpoint noise
from the previous run; use it for scaling, and this A/B for the comparison. DECISIONS AD18–AD21
and AD23 give the reasoning.

## 6. End to end through n8n

Workflow B on 10k real-profile users, per node (`scripts/n8n_execution.py`):

| | Before this phase | After |
|---|---|---|
| Code-node runner cold start | 2.2 s (the runner exited after 15 idle seconds) | 19 ms (kept warm, AD24) |
| Gemini, when a cohort or criterion is new | the first batch waits, 7.5 s per call | made by the warm step after the crawl (AD25): batches make 0 calls |
| Write matches | 5.8 s | 1.7–2.4 s |
| **Webhook to `matched`, everything cached** | ≈ 8.5 s | **2.1–2.7 s** |
| **First batch after a product change** | ≈ 8.5 s plus 2 Gemini calls (7–15 s each, more on 503s) | **3.05 s, 0 calls** (synthetic products, 50,209 questions; `make warm-demo`) |
| Most items held by any node | 1 | 1 (with LLM work: the number of requests, 2 for the real data) |

## 7. Scaling

`make bench`, every run checked against the brute-force reference (recall and precision 1.000
in all of them); LLM calls are counted, not spent.

| Products | Users (profile) | Naive LLM calls (N·M) | After S2 | Pending questions | Cohorts | LLM calls, nothing cached | Warm step calls | Calls after warming | Rerun calls | Write | Rerun write | Recall / precision |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| real (32) | 1,000 (real) | 32,000 | 23,855 | 356 | 1 | 2 | 2 | 0 | 0 | 0.36 s | 0.13 s | 1.000 / 1.000 |
| real (32) | 10,000 (real) | 320,000 | 236,462 | 3,392 | 1 | 2 | 2 | 0 | 0 | 2.06 s | 0.56 s | 1.000 / 1.000 |
| real (32) | 100,000 (real) | 3,200,000 | 2,366,262 | 33,974 | 1 | 2 | 2 | 0 | 0 | 26.74 s | 11.23 s | 1.000 / 1.000 |
| real (32) | 1,000 (wide) | 32,000 | 16,321 | 135 | 1 | 2 | 2 | 0 | 0 | 0.14 s | 0.08 s | 1.000 / 1.000 |
| real (32) | 10,000 (wide) | 320,000 | 164,652 | 1,464 | 1 | 2 | 2 | 0 | 0 | 0.83 s | 0.27 s | 1.000 / 1.000 |
| real (32) | 100,000 (wide) | 3,200,000 | 1,648,290 | 14,342 | 1 | 2 | 2 | 0 | 0 | 9.65 s | 7.48 s | 1.000 / 1.000 |
| synthetic (25) | 1,000 (real) | 25,000 | 11,601 | 5,064 | 19 | 2 | 2 | 0 | 0 | 0.20 s | 0.07 s | 1.000 / 1.000 |
| synthetic (25) | 10,000 (real) | 250,000 | 115,896 | 50,489 | 19 | 2 | 2 | 0 | 0 | 1.30 s | 0.42 s | 1.000 / 1.000 |
| synthetic (25) | 100,000 (real) | 2,500,000 | 1,156,519 | 503,545 | 19 | 2 | 2 | 0 | 0 | 10.52 s | 9.99 s | 1.000 / 1.000 |
| synthetic (25) | 1,000 (wide) | 25,000 | 5,335 | 1,904 | 19 | 2 | 2 | 0 | 0 | 0.06 s | 0.03 s | 1.000 / 1.000 |
| synthetic (25) | 10,000 (wide) | 250,000 | 54,784 | 19,885 | 19 | 2 | 2 | 0 | 0 | 0.41 s | 0.18 s | 1.000 / 1.000 |
| synthetic (25) | 100,000 (wide) | 2,500,000 | 550,953 | 200,533 | 19 | 2 | 2 | 0 | 0 | 6.03 s | 4.29 s | 1.000 / 1.000 |

With nothing cached, a batch needs 2 calls at every size (one cohort call for up to 25 cohorts,
one compile call for up to 25 criteria); the cohort count does not move with N. The warm step
(section 8) makes those same 2 calls right after the crawl instead, and a batch then needs 0.
"Rerun write" replays the same batch: every user is a returning user and nothing changed, so
nothing is written. The 100k timings vary by ±50% between runs on this machine (WAL and
checkpoints from the previous write); section 5 has the controlled comparison.

The same runs, timing the four exact S2 implementations (all four match the reference on every
run; the JavaScript times exclude moving the users into the process):

| Users (products, profile) | d0 JS nested loop | d4 JS bitmask | d1 SQL join | d2 SQL cells |
|---|---|---|---|---|
| 1,000 (real, real) | 11 ms | 3 ms | 25 ms | 9 ms |
| 10,000 (real, real) | 35 ms | 11 ms | 43 ms | 57 ms |
| 100,000 (real, real) | 265 ms | 89 ms | 422 ms | 545 ms |
| 1,000 (real, wide) | 3 ms | 2 ms | 17 ms | 8 ms |
| 10,000 (real, wide) | 26 ms | 9 ms | 36 ms | 60 ms |
| 100,000 (real, wide) | 147 ms | 180 ms | 367 ms | 451 ms |
| 1,000 (synthetic, real) | 3 ms | 2 ms | 13 ms | 8 ms |
| 10,000 (synthetic, real) | 12 ms | 9 ms | 28 ms | 46 ms |
| 100,000 (synthetic, real) | 146 ms | 71 ms | 653 ms | 419 ms |
| 1,000 (synthetic, wide) | 2 ms | 2 ms | 8 ms | 5 ms |
| 10,000 (synthetic, wide) | 8 ms | 4 ms | 22 ms | 36 ms |
| 100,000 (synthetic, wide) | 73 ms | 38 ms | 295 ms | 431 ms |

## 8. Warming: taking the LLM off the batch path

The lazy design still made the *first* batch after a product change wait for Gemini. But the
cohort bound of section 2 depends on the products alone, so the cohorts can be judged before any
batch exists. `cohort_space(mode)` (migration 008) enumerates them:

- for each fuzzy criterion f of each active product, take the label set of every fact in D_f:
  employment over the product's allowed values, and each numeric fact over every value a passing
  user could have (the users table's CHECK ranges, clipped to the product's thresholds), mapped
  through the same `band_label()` that batches use;
- the cohorts are the product of those label sets, with fuzzy_signatures' exact signature
  expression. Income is sampled at the product minimum and every ₹1,000 up to ₹2,50,000; every
  band boundary of every mode is a multiple of ₹1,000 and the fine bands are open above
  ₹2,00,000, so the samples reach every label.

Under threshold banding a numeric fact has exactly one label per product, so the space is
{criteria} × {allowed employments}, and every element is produced as soon as one user of each
employment passes the product: **space = what batches produce**. Tests assert that coverage
(every signature of a real-profile and a wide-profile batch is in the space, for every mode) and
the threshold equality; the sizes match the independent enumeration of section 2 (19, 47, 211).

The "Warm match cache" sub-workflow runs at the end of every Workflow A crawl: one query finds
what has no cached verdict or rule, and the same Code nodes, gateway and SQL as Workflow B judge
it. Measured through n8n on the synthetic products: the warm step made **2** calls (19 cohorts,
12 criteria) and the next 10k batch made **0**, matched in 3.05 s with 12,519 pairs rejected by
the cached judgements. With nothing new it is one 6 ms query. `make bench` reports both numbers
per run (section 7: "warm" and "after warm"); `make warm-demo` repeats the n8n run.

## 9. Considered and not pursued

| Idea | Why not |
|---|---|
| Overlap the Gemini call with the write (async sub-workflow) | Moot since warming: batches no longer make LLM calls. |
| Lower thinking level | Latency is a fixed per-request cost on the free tier (V26); output and thinking tokens are small. |
| Semantic deduplication of criteria (embeddings) | Exact-wording keys already share verdicts across products (15 instances, 12 wordings); a false merge could drop pairs. |
| Judge only the users about to be emailed | Leaves most matches pending; cohorts already make judging cost independent of N. |
| UNLOGGED `matches`, narrower rows | Durability matters; narrowing (bytea hash, NULL caveats) would save about 5% for a schema change. |
| A classifier or embeddings instead of rules | The facts are structured numbers; rules are exact where a classifier would approximate (DECISIONS, considered and rejected). |

## 10. Limits

- One model (`gemini-3.5-flash-lite`) and one synthetic product set; the oracle sample is 109
  pairs plus 100 held out. The false-drop result is the strong one: none of the 154 pairs the
  oracle keeps was dropped, and all 50 pairs the strategy dropped were confirmed. Drop recall
  is a noisier estimate.
- Criteria phrased relative to a product ("income comfortably exceeds the minimum") can't be
  compiled and stay uncertain; their cohorts are shared only by products with the same range.
- The oracle is the LLM itself with full information, not ground truth about lenders; the
  pipeline's guarantee to users is "never hide a product the rules allow unless the LLM is
  confident it doesn't apply".

### Trade-offs accepted for speed

Most of the speed came from removing waste (a cold runner, a second index probe per row, WAL for
an index nobody needed, rewrites of unchanged rows) and costs nothing. These do cost something,
and were accepted deliberately:

| Trade-off | What it means | Why accepted | What would remove it |
|---|---|---|---|
| Errors are shared across a cohort | One verdict covers every user in the cohort (all 3,389 Kotak pairs share one). A wrong verdict, or a wrong `depends_on` from extraction, is wrong for thousands of pairs at once; per-pair judging would spread mistakes thinly | This sharing is what makes the call count independent of N. The held-out check found no such error (0 false drops in 154 oracle keeps) | A spot-check in the warm step: re-ask a few sampled users per cohort and replace verdicts they contradict (1–2 calls per crawl) |
| Cached answers never expire | Verdicts and rules are keyed by the criterion's wording and the cohort, not by model or prompt version. A mistake persists until the product's wording changes, and a better model would not re-judge anything | It is what makes reruns and later batches cost 0 calls | Add the prompt version to the cache key, so a version change re-judges once at the next crawl |
| Coarser answers by design | About 8% of the rejections a per-pair LLM would make go out as "may qualify" (held-out estimate; 29% on the first, criterion-balanced sample). Threshold bands also blur numbers inside a product's range, so many users get an "uncertain" caveat rather than a crisp yes | Recall against the hard rules is untouched, and a false keep only adds a "may qualify" line; finer answers cost false drops or many more calls (section 4) | Only per-pair judging, at 25,000× the calls |
| Weaker referential integrity on `matches` | The statement-level checks catch bad inserts and updates, but take no row locks, so a parent deleted while a write runs can slip through, and `TRUNCATE` bypasses them | Row-level foreign keys cost 51 s against 12 s at 100k users; nothing in the pipeline deletes users, products or batches | Real foreign keys, at about 5 µs per row per key |
| More moving parts | Two judges, a veto rule, a warm step, and `cohort_space()` repeating `fuzzy_signatures()`'s signature expression | Each piece was kept because it measured better; a coverage test fails if the two signature expressions drift apart | Fewer pieces, each removal costing calls or accuracy |
| Sort memory on RDS | The write raises `work_mem` to 64 MB for its one statement. On a `db.t4g.micro` (1 GB) two large writes at once would be tight; all timings above are from local Docker, and RDS will be slower in absolute terms | Keeps the 10k-user sort in memory | Make the value configurable and lower it on RDS (≈ 0.1–0.2 s slower at 10k users when the sort spills) |

Not traded: recall against the hard rules (every pair the stated rules allow is stored),
durability (no unlogged tables, commits wait for disk), least privilege (n8n still has no
DELETE), and the brief's requirement that the matching runs in n8n. The fixes in the last
column were considered and not made: they would add calls or time to the daily crawl without
speeding up batches.

## 11. Full review and re-test (2026-10-17)

Every run was repeated after the day's changes: the merged statements, JIT off, one branch per
banding mode, and parallel LLM requests.

| Run | Result |
|---|---|
| `make bench`: 12 runs, 1k-100k users, real and synthetic products, both profiles | Every funnel number unchanged (pairs, pending, cohorts 1/19, 2 calls, 0 on rerun, 0 after warming); recall and precision 1.000 in all 12; all four S2 variants match the reference |
| `make bench-llm` and `--holdout`, run with an invalid API key so any uncached request would fail | All 14 strategies replay from the cache with the same numbers (chosen: 2 calls, 0.963 / 0.706); held-out: 0 false drops |
| `make match-e2e`, 10k users, repeated | recall and precision 1.0000 every run |
| `make warm-demo` (2 Gemini calls) | the 2 requests ran in parallel, both done in 3.9 s; the next 10k batch made 0 calls, recall 1.0000 |
| `make warm-demo ARGS=--lazy` (B judges its own cohorts, 2 Gemini calls) | both requests in 5.2 s; webhook to matched 8.4 s including them (about 23.5 s when they ran back to back) |
| `make crawl ARGS="--reset hdfc-bank"` (1 Gemini call) | extraction through the rewritten gateway works, but the product came back under another name: open issue O1, fixed by AD30 |

**Blockers found and fixed**

| Finding | Before | After |
|---|---|---|
| JIT compiled Workflow C's plan statement every run (estimate 1.5M; V37) | plan 1.25 s, C 1.35-1.41 s | plan 0.27 s, C 0.5-0.8 s |
| A multi-line LLM error broke "Log LLM call" (`$n` inside a SQL comment; V35) | the gateway failed on the first Gemini 429 | logged as text; the build refuses `$n` in comments |
| The task runner starts lazily after an n8n restart | first batch +1.1-1.9 s | `make bootstrap` warms it with a no-op webhook |
| The match table outgrew the buffer cache (5.5M rows; AD28) | 10k write 7.65 s median | 3.10 s |
| LLM requests ran one at a time (V36, AD29) | 2 calls ≈ 2 × 7-10 s | 2 calls in parallel, ≈ one call |
| The gateway logged no call duration | latency unmeasurable | logged; it drives the K planner |

**Measured and left as they are**

- JIT on the write and replay statements: 1.61 vs 1.63 s (10k), 26.9 vs 26.8 s (100k), replay
  13.8 vs 13.5 s. Off, for predictability.
- The integrity trigger on `matches`: disabling it at 100k measured 16.8 s against 15.9 s (noise).
  `work_mem` 256 MB instead of 64 MB: 17.4 s (no gain).
- **Where a 100k write's time goes** (synthetic products, 1.15M pairs, about 16 s):
  - hard filter: 0.4-0.6 s;
  - decisions (two judges, 503k fuzzy questions): 4.2-4.6 s;
  - the insert of full-width rows: about 11 s, roughly 9 µs a row.

  With the real products (2.37M pairs, 34k fuzzy questions) the decisions take 1.0-1.2 s, and
  the insert is the rest.
- **The remaining structural lever is writing fewer rows.** A prototype stored one row per user,
  with the products in arrays, instead of one row per pair. Inserting 1.15M pairs that way took
  **1.1-1.4 s instead of 2.4-3.4 s** in bare tables, with 117 MB instead of 209 MB on disk. That
  is about 2-2.5× on the write, the step that dominates at scale. It changes the data model:
  - Workflow C's queries need rewriting;
  - so do the replay check and the bench's reference comparison;
  - "which users match product X" needs a GIN index.

  So it is a decision for the owner, not a tuning step.
- **100k timings on this machine vary by up to ±50% between identical runs.** A first committed
  100k write measured 53 s once and 15-30 s otherwise. Comparisons above are interleaved A/B runs
  for that reason.
- **Gemini is not fully deterministic at temperature 0.** Two fresh runs judging the same 19
  cohorts and 12 criteria rejected 11,486 and 8,153 pairs. Both runs had recall 1.0 against the
  hard rules; the difference is in the LLM's optional rejections. The cache is what makes an
  answer stick once given.

## 12. One row per user instead of one per pair (2026-10-17)

The section 11 review found the write path near its per-row floor (about 9 µs per pair row), so
the remaining lever was writing fewer rows. `user_matches` (DECISIONS AD31) stores each user's
pairs as sorted arrays, plus a small map for the few pairs with details. The comparison used
the current statements and the prototype's on the same database, users and decisions (the
prototype reuses the write statement's decision logic unchanged). The two layouts alternated,
each timed statement ran in a rolled-back transaction, and the tables already held an earlier
batch. Afterwards every pair's status, reason, caveats, score and decided_by were compared
between the layouts.

| Path | Real products, 10k | Real, 100k | Synthetic, 100k (48% of pairs with details) |
|---|---|---|---|
| Pairs compared (differences) | 213,446 (0) | 2,147,170 (0) | 1,155,914 (0) |
| B write, new users | 1.16 → **0.37 s** | 25.5 → **8.3 s** | 16.1 → **10.0 s** |
| B replay of an unchanged batch | 0.76 → **0.36 s** | 21.8 → **8.2 s** | 10.7 → **6.9 s** |
| B re-upload, same users, new batch | 3.90 → **0.48 s** | 140 → **14.1 s** | 30.8 → **11.6 s** |
| C plan (6 rounds) | 0.19 → **0.17 s** | 3.71 → **2.94 s** | 2.43 → **2.17 s** |
| C record, 2 emails (20 rounds, isolated) | 5.9 → 5.5 ms | 28.2 → 27.6 ms | 36.8 → 35.3 ms |
| WAL for the write | 74 → **9 MB** | 744 → **93 MB** | 478 → **145 MB** |
| Storage after two batches | 304 → **40 MB** | 1,915 → **320 MB** | 1,288 → **531 MB** |
| Reading a batch as pairs (view) | 0.03 → 0.01 s | 1.33 → 0.20 s | 0.71 → 0.31 s |

Every path ran faster or equally fast. Workflow C's plan was first slightly slower at 100k
(2.72 vs 2.56 s, 2 rounds), because it probed two primary keys per user. It now reads the
batch's own rows through the BRIN index, since a batch's rows sit side by side, and is faster
at every size. The record step first looked slower at 100k (75 vs 60 ms) when it ran after a
plan in the same transaction. Timed alone over 20 rounds it is equal.

Through n8n (10k real-profile users), webhook to matched went from 2.7-3.6 s to
**0.85-1.36 s**, and webhook to notified from 3.2-4.1 s to **1.10-1.87 s**, recall 1.0000.

