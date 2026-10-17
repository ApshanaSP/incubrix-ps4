# REPORT: Long-Tail Destination Network

## 1. Approach and why

**Contract first.** Each connector is one class with the Appendix A methods plus one manifest YAML, served at
`POST /connector/v1/{connector_key}/{method}` so IncuBrix's Connector Runtime can load it unchanged. Connectors only
return results and raise the eight shared codes (a test enforces it). `python -m tools.new_connector <key>` makes a
new one from the template that certifies on the mock platforms at once.

**One guarded publish path** (`app/publisher.py`), not safety code in every connector:
1. **Idempotency ledger:** a key that already produced a post returns that post.
2. **Certification gate:** only certified destinations are used; demoted and paused ones are excluded.
3. **Connection state:** an expired or revoked connection blocks the post.
4. **Format limits and posting rules** (`app/rules.py`): block or reschedule, always with the reason.
5. **The attempt is written as `in_flight` before the platform call.**
6. **After a timeout, find out:** `find_existing` looks for the post before posting again; if it cannot tell, the
   service refuses to post blind. This is what makes "the same key never posts twice" hold on platforms with no
   idempotency support.

**Configuration, not code.** A destination row holds the instance parameters (`base_url`, `community`, `channel`,
`publication_id`), so forum number 41 is one row. **Simulation exercises the real code:** connector HTTP calls are
routed to mock platforms that follow each real API (paths, fields, errors, rate-limit headers), and fault injection
reproduces every hard case on demand. Reading public pages (rules, reachability) is never simulated.

| Alternative | Why not |
|---|---|
| Queue + workers (Celery/Redis) | More moving parts for 10 days; an in-process job runner with SQLite meets the standard (202 + job_id, progress, signed callbacks) and is the seam for SQS later |
| Postgres | SQLite in WAL mode with one writer is enough and keeps compose small; the store is one module |
| Rules inside each connector | They would drift, and connectors must not touch IncuBrix state |
| AI-written tailoring | Deterministic tailoring cannot invent claims and is easy to test; AI is used where judgement is needed (reading rules, relevance), behind an adapter, with a person confirming rules |

## 2. What works

| Area | Status | Proof |
|---|---|---|
| Contract, 9 connectors (6 Core + Hashnode, Slack app, MRSS) | All methods, manifests and error codes | `tests/test_contract.py` |
| Certification: publish, live URL, status, metrics, delete, deletion confirmed | All 9 certified on the mock platforms; real Lemmy 0.19.5 (L4) and Ghost 5 (L3) under `docker compose` | `certification_pack/INDEX.md`, `outputs/metrics/local_instances.json` |
| Connectors against the real platforms (read-only) | Discourse, Dev.to and Lemmy read real public posts with their own code; Hashnode's API now needs a paid plan and the connector says so | `outputs/metrics/real_api_check.json` |
| Rules before every post | Frequency cap, self-promotion (flagged or detected), links, AI disclosure, automation, language | `tests/test_rules.py`, rule test set |
| Safe retries | 0 duplicates under timeouts; Idempotency-Key replay on every POST | `tests/test_failures.py`, batch input 02 |
| Hard cases 1-7 | Each handled with an explicit code and reason | batch inputs 02-15 (17/17), README table |
| Catalogue import, AI rules capture with confirmation, relevance, tailoring | Done | `tests/test_catalogue.py`, batch inputs 12 and 16 |
| Integration Standard 1-10 | All 10 | `tests/test_integration.py` |
| One command, no keys | 89 tests (`python -m pytest`, or `docker compose run --rm api python -m pytest`) | `test_pack/tests/` |
| Independent reviewer-style check over HTTP | 38/38 in light mode and against `docker compose` | `test_pack/reviewer_check/`, `outputs/compose/reviewer_check/` |
| Dashboard at `/` | Destinations and testing, publishing with outcomes in plain words, the 17 hard cases from the browser, CSV import | `app/static/dashboard.html` |

## 3. What does not work yet, and why

- **Most Discourse forums' rules cannot be read automatically.** Discourse's default robots.txt line `Disallow: /g`
  also covers `/guidelines` (and `/faq` redirects there); we respect it. Those rows keep strict defaults until a
  person pastes the rules (`catalogue/pasted_rules/`, verified the same way). Reddit forbids automated reading too.
- **The first catalogue builds read our own mock site instead of real rules pages** (simulation mode routed page
  reads to the mock). Found on 17 Oct from identical quotes across forums; fixed so reading is never simulated,
  the builder refuses mock text, and every AI quote must be found on its page. The catalogue was rebuilt.
- **Only Lemmy and Ghost have been posted to on real software.** Discourse, Dev.to and Lemmy were checked
  read-only on real platforms; Slack and the partner webhook only against mocks. Certification is the guard on
  first real use. **Hashnode retired its free API on 13 May 2026**; posting there needs a Pro plan.
- **Self-promotion detection is rule-based** (strong and weak signals, negation, affiliate patterns, own domain).
  Probing found and fixed four mistakes; cleverly worded promotion can still pass. The rule test set is ours, so
  its 0 misses show the rules work as designed, not on unseen wording.
- **The recommender matches words, not meaning** (TF-IDF topic match + audience + rules + past results + format).
  Its labels are a first draft made with AI help before rows were added (see AI_USE.md).
- **Timeout recovery matches on title** on Discourse, Lemmy, Dev.to, Ghost and Hashnode (exact on Slack and the
  partner webhook). **Dev.to has no delete API** (delete unpublishes). **Ghost has no view counts** (L3).
- **Single-instance job runner:** jobs running at a restart are marked failed (retryable), not resumed.

## 4. Measured numbers

Dell Latitude E7470 (i7-6600U, 2 cores, 16 GB), Windows 11, Python 3.11.5, simulation mode unless stated.
`python -m tools.measure --runs 5 --publishes 300` → `outputs/metrics/metrics.json`.

| Metric | Result |
|---|---|
| Certification | 9 connectors × 5 fresh destinations = **45/45**, 0.006-0.019 s per run on mocks; real Lemmy 1.2 s (L4), real Ghost 3.6 s (L3) |
| Reliability (simulation) | 300 publishes, about 45% with injected faults (timeout after create, timeout, 429, 500, reject): **250/250 published** after same-key retries (30 rejections and 20 long-429 reschedules correctly not retried); **0 duplicate posts**, including 22 timeouts after the post was created |
| Reliability (real local instances) | Lemmy 10/10 and Ghost 10/10 published, live (HTTP 200), same key returned the same post 10/10, all deleted |
| Rule safety | 51 cases: **31/31 violations caught, 0 missed, 0 false blocks** |
| Catalogue import | 1,000 rows online in **19.5 s** (50/50 duplicates, 10/10 bad URLs, 723 unreachable reported, slowest `/health` during the import 0.67 s); offline 1,002 rows in 3.2 s, 5,002 in 17.2 s |
| Recommender | precision@10 **0.32** on the draft labels (0.17 before the topic match and the assisted route); per item 0.0-0.7 |
| Cost | **$0 per 1,000 publications** (no AI on the publish path); AI rules reading about $0.0004-0.0006 per destination at Groq list prices; everything ran on free tiers, $0 spent |

**Real catalogue** (`catalogue/real_catalogue.csv`): 255 real destinations, **185 outside Reddit** (Reddit is a
major platform, so it does not count toward the long tail), **139 relevant** (score 3+). 49 Discourse, 37 Lemmy,
10 Dev.to tags, 5 Hashnode tags, 8 Slack communities, 76 assisted (launch sites, directories, Q&A, PeerTube,
forums). **67 have rules read from their real rules page with every quote checked against it** (106 of 114 AI
quotes found); the rest have strict defaults marked `conservative_default`. Every row has a relevance score with a
reason and a connector type. Nothing was posted to any of them. `catalogue/pasted_rules/TODO.csv` lists the 92
relevant destinations still waiting for rules.

## 5. Outstanding items

- **Recommender:** ranked with a reason per destination; rule-breakers excluded with the reason; places that ban
  automation are routed to a person's ready-to-post pack instead of dropped.
- **Metrics:** `fetch_metrics` on Discourse, Lemmy, Dev.to, Hashnode, Slack and the webhook, normalised to views,
  likes, comments, shares, saves, watch time and clicks.
- **Recertification:** a scheduler re-tests destinations certified more than 25 days ago; a failure demotes and
  excludes the destination, opens a ticket and calls `ALERT_WEBHOOK_URL`.
- **Real catalogue:** see section 4. It is **partial** against "150 researched with rules": 139 relevant, 67 with
  verified rules.

## 6. What I would do next

1. Paste and verify rules for the 92 relevant destinations in `TODO.csv`, then label the recommender set by hand.
2. Jobs on SQS and the store on Postgres or DynamoDB, so several API containers can run.
3. An exact idempotency marker inside posts where platforms allow hidden metadata.
4. An AI promotion classifier behind the provider adapter, measured on wording we did not write.
5. PeerTube and Kit/Mailchimp connectors from the template; OAuth for Slack installation.
