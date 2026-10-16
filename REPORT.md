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
| Real local Lemmy 0.19 and Ghost 5 | Compose and bootstrap written; **see section 3** | `docker-compose.yml`, `tools/bootstrap_local.py` |
| Rules before every post | Frequency cap, self-promotion (explicit and detected), link policy, AI disclosure, automation, language | `tests/test_rules.py`; rule test set below |
| Safe retries | Timeout-after-create recovered with 0 duplicates; Idempotency-Key replay on every POST | `tests/test_failures.py`, batch input 02 |
| Hard cases 1-7 | All handled with explicit codes and reasons | batch inputs 02-15, README table |
| Catalogue import | Validate, de-duplicate (normalised URL + platform), reachability, enrichment (Discourse/Lemmy member counts), language detection, relevance, row-by-row report | `tests/test_catalogue.py`, batch input 12 |
| AI rules capture with human confirmation | Groq `gpt-oss-120b` proposes rules with quotes; posting is blocked until a person confirms | batch input 16 |
| Relevance 1-5 with a reason | AI or heuristic | `/api/v1/relevance` |
| Tailoring | Forum discussion-first, canonical URL for articles, word-boundary truncation, tag and hashtag style, mentions, localised AI disclosure | `tests/test_rules.py`, `tests/test_catalogue.py::test_tailoring_styles` |
| Recommender, metrics, recertification, real catalogue | See sections 4 and 5 | |
| Integration Standard 1-10 | All 10 | `tests/test_integration.py` |

## 3. What does not work yet, and why

- **The real Lemmy and Ghost runs depend on Docker**, which was not installed on the development laptop until late. <!-- UPDATE after the compose run: results, or what failed. -->
- **Dev.to has no delete API.** `delete` unpublishes the article, which removes it from all public pages. This is reported as `method: unpublished`.
- **Reddit, Hacker News and most large communities do not allow automated posting.** They are assisted destinations: the service builds the pack and a person posts it. Reddit's `robots.txt` also forbids automated reading, so its rules are never fetched. Those rows carry strict defaults until a person pastes the rules (`rules_text`).
- **Lemmy-UI and many forums render their rules with JavaScript.** Lemmy rules are read through its API instead. Some Discourse forums disallow `/guidelines` in `robots.txt`; those rows fall back to strict defaults, marked `rules_source=conservative_default`.
- **Ghost's Admin API has no view counts**, so Ghost has no `fetch_metrics` and certifies at L3, not L4.
- **Reconciliation after a timeout matches on title for platforms without metadata** (Discourse, Lemmy, Dev.to, Ghost, Hashnode). Two different posts with the same title inside the lookup window could be confused. Slack (message metadata) and the partner webhook (idempotency-key lookup) are exact.
- **Single-instance job runner.** Jobs that were running when the process stopped are marked failed on restart (`retryable: true`), not resumed.

## 4. Measured numbers

Hardware: Dell Latitude E7470 laptop, Intel Core i7-6600U (2 cores, 4 threads, 2.6 GHz), 16 GB RAM, Windows 11, Python 3.11.5. Simulation mode with the mock platforms in-process. Command: `python -m tools.measure --runs 5 --publishes 300`. Raw output: `outputs/metrics/metrics.json`.

**Certification** (5 fresh destinations per connector, all passed):

| Connector | Certified | Mean time per run | Max |
|---|---|---|---|
| discourse | 5/5 | 0.038 s | 0.062 s |
| lemmy | 5/5 | 0.025 s | 0.031 s |
| devto | 5/5 | 0.016 s | 0.016 s |
| ghost | 5/5 | 0.019 s | 0.031 s |
| webhook | 5/5 | 0.016 s | 0.032 s |
| assisted | 5/5 | 0.022 s | 0.032 s |
| hashnode | 5/5 | 0.016 s | 0.016 s |
| slack | 5/5 | 0.025 s | 0.031 s |
| mrss | 5/5 | 0.034 s | 0.110 s |

Test posts are deleted in the same run, so the 10-minute cleanup limit is met with a wide margin. Real network times will be higher. <!-- UPDATE with the local Lemmy/Ghost times. -->

**Reliability:** 300 publishes across the 7 native connectors with random injected faults. About 45% of calls had a fault: timeout after the post was created, timeout before, 429 with a 1-second or 10-minute reset, 500, and rejection. Each failure was retried through `/posts/{id}/retry` with the same idempotency key.

- 250 published; 30 rejected by the platform as injected (correct: not retried); 20 rescheduled to the 10-minute reset time (correct: not retried early).
- Success rate on the rest: **100%** (250/250).
- **Duplicate posts found on the platforms: 0**, including the 22 cases where the platform created the post and the answer never arrived.

**Rule safety** (`catalogue/rule_test_set.json`, 44 labelled cases: 27 violations, 17 allowed posts):

- **27 caught, 0 missed.**
- 0 false blocks.

**Catalogue import** (offline reachability, to time the pipeline itself):

- 1,002 rows (50 duplicates, 2 invalid): 5.3 s.
- 5,002 rows: 19.1 s.
- With online reachability at concurrency 50 and a 6 s timeout, the worst case for 1,000 unreachable rows is about 2 minutes. <!-- UPDATE: online timing of the real catalogue import. -->

**Recommender:** precision of the top 10 on the labelled set: <!-- UPDATE -->

**Cost:**

- Publishing, tailoring and rule checks make no paid calls: **$0 per 1,000 publications**.
- AI rules capture with Groq `openai/gpt-oss-120b` measured on real rules pages: about 840 input and 600 output tokens per page, about **$0.0006 per destination** at list prices ($0.15 / $0.75 per million tokens). Run on the free tier during development, so $0 actually spent.
- The real-catalogue build: <!-- UPDATE: tokens and cost from catalogue/real_catalogue_evidence.json. -->

## 5. Outstanding items

- **Recommender.** Ranks by audience fit, rules fit (rule-breakers are excluded with the reason), past engagement from `fetch_metrics`, and format fit, with a reason for each destination.
- **Metrics.** `fetch_metrics` works on Discourse, Lemmy, Dev.to, Hashnode, Slack and the partner webhook, normalised to views, likes, comments, shares, saves, watch time and clicks.
- **Recertification.** A scheduler (`RECERT_INTERVAL_S`) re-tests destinations last certified more than `RECERT_AFTER_DAYS` (25) ago. A failure demotes the destination to L0, excludes it, opens a ticket and calls `ALERT_WEBHOOK_URL`.
- **Real catalogue.** <!-- UPDATE: counts by rules source and connector. -->

## 6. What I would do next

1. Move jobs to SQS (IncuBrix already runs on AWS) and the store to Postgres or DynamoDB, so several API containers can run.
2. Store an idempotency marker inside the post where the platform allows hidden metadata (Ghost `codeinjection_foot`, Discourse custom fields through a plugin), to make timeout recovery exact everywhere.
3. A small reviewer UI for confirming AI-proposed rules in bulk, showing each quote next to the rule.
4. PeerTube and Kit/Mailchimp connectors; OAuth flows for Slack app installation.
5. Learn the recommender weights from real engagement once there is enough history.
