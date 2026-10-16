# REPORT: Long-Tail Destination Network

## 1. Approach and why

**One service, contract first.** Every connector is a class implementing the Appendix A methods, plus one manifest YAML file. They are served exactly at `POST /connector/v1/{connector_key}/{method}` (mirrored under `/api/v1/connector/`), so IncuBrix's Connector Runtime can load them unchanged. Connector methods only return results and raise the eight shared codes. A test checks that no connector maps an error to any other code.

**The guarded publish path.** All safety decisions sit in one place, `app/publisher.py`, and not inside each connector:

1. **Idempotency ledger.** A key that already produced a post returns that post.
2. **Certification gate.** Only certified destinations are used. Demoted and paused ones are excluded.
3. **Connection state.** An expired or revoked connection blocks the post.
4. **Format limits and posting rules.** A breaking post is blocked or rescheduled, with the reason.
5. **The attempt is written as `in_flight` before the platform call.**
6. **After a timeout, find out.** The connector's `find_existing` looks for the post (Slack message metadata, partner lookup by idempotency key, the account's recent posts by title on the other platforms). If the post exists, it is returned. If the lookup itself fails, the service refuses to post blind.

This is what makes "the same key never creates a second post, even after a timeout" hold for platforms that have no idempotency support of their own.

**Configuration, not code.** The destination row holds the instance parameters: `base_url`, `community`, `channel`, `feed_key`, `publication_id`. A connector never knows which forum it talks to until it is called. The mock platforms follow the same idea: each host is its own mock instance, so any number of simulated forums need no code.

**Simulation that exercises the real code.** In simulation mode the connector's real HTTP request is routed to mock platforms that follow each real API: same paths, field names, error bodies and rate-limit headers. The connector code is identical in both modes. Fault injection (`/api/v1/sim/faults`) reproduces every hard case on demand, so the hidden-test situations can be shown live.

**Alternatives considered:**

| Option | Why not |
|---|---|
| A queue plus separate workers (Celery or Redis) | More moving parts for a 10-day build. An in-process async job runner with SQLite persistence meets the Integration Standard (202 + job_id, progress, signed callbacks). The job interface is the seam for moving to SQS or Redis later. |
| Postgres for the service | SQLite in WAL mode with a single writer is enough here and keeps compose small. The store is one module (`app/db.py`). |
| Letting each connector enforce rules | Rules would drift between connectors, and the contract says connectors must not touch IncuBrix state. The rules engine is shared and runs before every post. |
| AI for tailoring | Deterministic tailoring is predictable, cannot invent claims, and is easy to test. AI is used where judgement is needed (reading rules pages, relevance), always behind an adapter and with a person confirming the rules. |

## 2. What works

| Area | Status | Proof |
|---|---|---|
| Contract (methods, manifest, error codes) | Done for all 9 connectors | `tests/test_contract.py`: manifest shape, methods, HTTP round trip per connector |
| Core connectors: Discourse, Lemmy, Dev.to, Ghost, signed webhook, assisted | Certified in simulation (publish, confirm live URL, status, metrics, delete, confirm deletion) | `tests/test_certification.py`, batch input 01, evidence JSON per run |
| Advanced connectors: Hashnode, Slack app, MRSS | Certified in simulation | the same |
| Real local Lemmy 0.19.5 and Ghost 5 | `docker compose up` brings up api, mocks, S3, Lemmy (+UI, Postgres) and Ghost; bootstrap certifies all 14 destinations: local Lemmy at L4, local Ghost at L3 | `docker compose logs bootstrap`, `outputs/metrics/local_instances.json` |
| Tests in the container | 79/79 pass: `docker compose run --rm api python -m pytest` | `test_pack/tests/` |
| Batch runner against the compose stack (mocks as a separate service) | 17/17 inputs as expected | `outputs/compose/batch-20261016-202242/` |
| Light mode, no Docker (`start-light.ps1`: one process, mocks in-process, about 75 MB of RAM) | 84/84 tests pass; batch runner 17/17 inputs as expected | `outputs/light/batch-20261016-231014/` |
| Reviewer-style check over HTTP (`tools/reviewer_check.py`): every must-have, Integration point and hard case with inputs that differ from the batch inputs | 38/38 checks pass | `outputs/reviewer_check/` |
| Certification pack: one destination per connector, run record + evidence page + screenshot, plus the real Lemmy and Ghost runs | 9/9 connectors passed | `certification_pack/INDEX.md` |
| Rules before every post | Frequency cap, self-promotion (explicit and detected), link policy, AI disclosure, automation, language | `tests/test_rules.py`; rule test set below |
| Safe retries | Timeout-after-create recovered with 0 duplicates; Idempotency-Key replay on every POST | `tests/test_failures.py`, batch input 02 |
| Hard cases 1-7 | All handled with explicit codes and reasons | batch inputs 02-15, README table |
| Non-English communities | A post in another language is held with the reason unless the creator's translation is given (`content.translations`, then posted in that language) or the community accepts the language | `tests/test_rules.py`, batch input 14 |
| Places that ban automation | No bot posts there: the post is routed to the assisted tier and a person gets the ready-to-post pack; every other rule still applies | `tests/test_rules.py::test_automation_not_allowed_makes_a_pack_for_a_person` |
| Catalogue import | Validate, de-duplicate (normalised URL + platform), reachability, enrichment (Discourse/Lemmy member counts), language detection, relevance, row-by-row report | `tests/test_catalogue.py`, batch input 12 |
| AI rules capture with human confirmation | Groq `gpt-oss-120b` proposes rules with quotes; posting is blocked until a person confirms | batch input 16 |
| Relevance 1-5 with a reason | AI or heuristic | `/api/v1/relevance` |
| Tailoring | Forum discussion-first, canonical URL for articles, word-boundary truncation, tag and hashtag style, mentions, localised AI disclosure | `tests/test_rules.py`, `tests/test_catalogue.py::test_tailoring_styles` |
| Recommender, metrics, recertification, real catalogue | See sections 4 and 5 | |
| Integration Standard 1-10 | All 10 | `tests/test_integration.py` |

## 3. What does not work yet, and why

- **First compose runs failed, and the fixes are in the history.** MinIO no longer publishes Docker Hub images, so storage moved to SeaweedFS's S3 gateway. Ghost rejected the first local owner password as insecure, wanted to email new-device codes (there is no mail server locally), and returned its admin key in an unexpected form. The bootstrap's failed Ghost certification is kept in the certification history. A first batch run against compose hit the API's own rate limit (120 requests a minute), and its partial output is kept in `outputs/compose/batch-20261016-202108/`.
- **Dev.to has no delete API.** `delete` unpublishes the article, which removes it from all public pages. This is reported as `method: unpublished`.
- **Reddit, Hacker News and most large communities do not allow automated posting.** They are assisted destinations: the service builds the pack and a person posts it. Reddit's `robots.txt` also forbids automated reading, so its rules are never fetched. Those rows carry strict defaults until a person pastes the rules (`rules_text`).
- **Lemmy-UI and many forums render their rules with JavaScript.** Lemmy rules are read through its API instead. Some Discourse forums disallow `/guidelines` in `robots.txt`; those rows fall back to strict defaults, marked `rules_source=conservative_default`.
- **Ghost's Admin API has no view counts**, so Ghost has no `fetch_metrics` and certifies at L3, not L4.
- **Reconciliation after a timeout matches on title for platforms without metadata** (Discourse, Lemmy, Dev.to, Ghost, Hashnode). Two different posts with the same title inside the lookup window could be confused. Slack (message metadata) and the partner webhook (idempotency-key lookup) are exact.
- **Docker compose was last run on 16 Oct.** It was verified then with real Lemmy and Ghost (section 4). The PC used for this work has a failing SSD (repeated crashes since September), so compose was not re-run after the 17 Oct changes; they were tested in light mode and by the test suite. Re-run `docker compose up` on another machine before relying on it.
- **Recommender labels are a first draft.** `catalogue/recommender_labels.csv` was drafted with AI help (see AI_USE.md) before 70 rows were added to the catalogue, so some good recommendations (for example the new launch sites for a launch post) count as misses. The precision below is measured on that draft.
- **Antivirus that re-signs HTTPS** (Avast on the development PC) breaks certificate checks unless `USE_SYSTEM_CERTS=true`; light mode sets it.
- **Single-instance job runner.** Jobs that were running when the process stopped are marked failed on restart (`retryable: true`), not resumed.

## 4. Measured numbers

Hardware: Dell Latitude E7470 laptop, Intel Core i7-6600U (2 cores, 4 threads, 2.6 GHz), 16 GB RAM, Windows 11, Python 3.11.5. Simulation mode with the mock platforms in-process. Command: `python -m tools.measure --runs 5 --publishes 300`. Raw output: `outputs/metrics/metrics.json`.

**Certification** (5 fresh destinations per connector, all passed):

| Connector | Certified | Mean time per run | Max |
|---|---|---|---|
| discourse | 5/5 | 0.019 s | 0.047 s |
| lemmy | 5/5 | 0.013 s | 0.016 s |
| devto | 5/5 | 0.015 s | 0.016 s |
| ghost | 5/5 | 0.013 s | 0.016 s |
| webhook | 5/5 | 0.006 s | 0.015 s |
| assisted | 5/5 | 0.009 s | 0.015 s |
| hashnode | 5/5 | 0.006 s | 0.016 s |
| slack | 5/5 | 0.012 s | 0.016 s |
| mrss | 5/5 | 0.009 s | 0.016 s |

Test posts are deleted in the same run, so the 10-minute cleanup limit is met with a wide margin.

**Real local instances** (docker compose on the same laptop, Docker Desktop 29.8; `python -m tools.measure_local --n 10`, raw output in `outputs/metrics/local_instances.json`):

| Destination | Certification run | Publishes | Published | Same key again returns the same post | Live URL answered 200 | Deleted afterwards | Mean publish job |
|---|---|---|---|---|---|---|---|
| Local Lemmy c/incubrix_test | 0.7-1.6 s, L4 | 10 | 10 | 10/10 | 10/10 | 10/10 | 0.41 s |
| Local Ghost blog | 4.3 s, L3 | 10 | 10 | 10/10 | 10/10 | 10/10 | 0.39 s |

**Reliability:** 300 publishes across the 7 native connectors with random injected faults. About 45% of calls had a fault: timeout after the post was created, timeout before, 429 with a 1-second or 10-minute reset, 500, and rejection. Each failure was retried through `/posts/{id}/retry` with the same idempotency key.

- 250 published; 30 rejected by the platform as injected (correct: not retried); 20 rescheduled to the 10-minute reset time (correct: not retried early).
- Success rate on the rest: **100%** (250/250).
- **Duplicate posts found on the platforms: 0**, including the 22 cases where the platform created the post and the answer never arrived.

**Rule safety** (`catalogue/rule_test_set.json`, 45 labelled cases: 28 violations, 17 allowed posts):

- **28 caught, 0 missed.**
- 0 false blocks.

**Catalogue import** (offline reachability, to time the pipeline itself):

- 1,002 rows (50 duplicates, 2 invalid): 3.2 s.
- 5,002 rows: 17.2 s.

**Catalogue import, online** (live reachability with robots.txt respected; `python -m tools.measure_import_online`, raw output in `outputs/metrics/catalogue_import_online.json`):

| File | Rows | Time | Duplicates found | Errors | Unreachable | Slowest /health during the import |
|---|---|---|---|---|---|---|
| `catalogue/real_catalogue.csv` | 237 | 18.6 s | 0 | 0 | 19 | 0.64 s |
| 1,000 rows: the real rows + 50 duplicates (other case, `www.`, trailing slash, tracking parameter) + 10 bad URLs + 703 rows on `.invalid` hosts | 1,000 | 19.5 s | 50 of 50 | 10 of 10 | 723 | 0.67 s |

An earlier version froze the API for 10 s during an online import (a new TLS context per host for robots.txt, and charset guessing over whole pages); both were fixed and the numbers above are after the fix.

**Recommender:** mean precision of the top 10 over 10 content items on the draft labelled set (`python -m tools.eval_recommender`, `outputs/metrics/recommender.json`): **0.33**. It was 0.17 before two fixes: a topic match (TF-IDF between the post and each destination's description) and recommending places that ban automation through the assisted pack instead of dropping them. Per item it ranges from 0.0 (a speech-to-text post, where the labels expect general ML forums whose descriptions never mention speech) to 0.7. See section 3 on the labels.

**Cost:**

- Publishing, tailoring and rule checks make no paid calls: **$0 per 1,000 publications**.
- AI rules capture with Groq `openai/gpt-oss-120b` measured on real rules pages: about 840 input and 600 output tokens per page, about **$0.0006 per destination** at list prices ($0.15 / $0.75 per million tokens). Run on the free tier during development, so $0 actually spent.
- The real-catalogue rebuild (17 Oct): 72 calls to Groq `openai/gpt-oss-20b`, 61,181 input and 68,842 output tokens, **$0.025 at list prices** (`catalogue/real_catalogue_evidence.json`); 18 rows reuse answers from the first build with `gpt-oss-120b`. Run on the free tier, so $0 spent. Rows whose rules page could not be read make no AI call.

## 5. Outstanding items

- **Recommender.** Ranks by audience fit, rules fit (rule-breakers are excluded with the reason), past engagement from `fetch_metrics`, and format fit, with a reason for each destination.
- **Metrics.** `fetch_metrics` works on Discourse, Lemmy, Dev.to, Hashnode, Slack and the partner webhook, normalised to views, likes, comments, shares, saves, watch time and clicks.
- **Recertification.** A scheduler (`RECERT_INTERVAL_S`) re-tests destinations last certified more than `RECERT_AFTER_DAYS` (25) ago. A failure demotes the destination to L0, excludes it, opens a ticket and calls `ALERT_WEBHOOK_URL`.
- **Real catalogue.** `catalogue/real_catalogue.csv`: 237 real destinations, **167 outside Reddit** (Reddit is one of the 15 major platforms, so it is not counted toward the long tail). Outside Reddit: 49 Discourse, 24 Lemmy, 10 Dev.to tags, 5 Hashnode tags, 8 Slack communities, 71 assisted (launch sites, directories, Q&A, PeerTube, other forums); 83 have rules read from their rules page by the AI with a quote for each rule (`real_catalogue_evidence.json`), 84 have strict conservative defaults marked `rules_source=conservative_default`. Languages: English plus German, French, Spanish, Portuguese, Japanese and Korean communities. Each row has a relevance score 1-5 with a reason and a connector type. Nothing was posted to any of them.

## 6. What I would do next

1. Move jobs to SQS (IncuBrix already runs on AWS) and the store to Postgres or DynamoDB, so several API containers can run.
2. Store an idempotency marker inside the post where the platform allows hidden metadata (Ghost `codeinjection_foot`, Discourse custom fields through a plugin), to make timeout recovery exact everywhere.
3. A small reviewer UI for confirming AI-proposed rules in bulk, showing each quote next to the rule.
4. PeerTube and Kit/Mailchimp connectors; OAuth flows for Slack app installation.
5. Learn the recommender weights from real engagement once there is enough history.
