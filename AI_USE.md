# AI use

## AI coding tools

| Tool | Used for |
|---|---|
| Claude Code (desktop app, model Claude Opus 5.5) | Most of the code was generated in a pair-programming session that I directed: the service skeleton, connectors, rules engine, mocks, tests, batch runner, compose file and first drafts of the docs. I chose the problem statement, set the requirements from the brief, reviewed the design choices listed in REPORT.md, and ran and checked the results. |

Commits made in that session carry a `Co-Authored-By: Claude` line, so the history shows which changes were AI-assisted.

Also drafted with Claude Code: the content items in `catalogue/recommender_eval_items.json` and the first draft of the relevance labels in `catalogue/recommender_labels.csv`. <!-- Candidate: say here that you reviewed and corrected the labels, and how many you changed, before quoting the precision@10 number. -->

Light mode (`start-light.ps1`, no Docker) and the batch runner's handling of the service's own rate limit were added in a later Claude Code session.

<!-- Candidate: add anything else you used (ChatGPT, Copilot...) and what you changed by hand. -->

## AI inside the product

| Task | Default | Optional providers | Human control |
|---|---|---|---|
| Rules capture: read a community's rules page and propose posting rules with a quote for each | heuristic adapter (no key) | Groq `openai/gpt-oss-120b`, Gemini 2.5 Flash | The proposal is stored as `proposed` and posting is blocked until a person confirms or corrects it (`POST .../rules/{id}/confirm`) |
| Relevance score 1-5 with a reason | heuristic adapter | same | Scores can be overridden per destination |
| Building the real catalogue (`tools/build_real_catalogue.py`) | Groq `openai/gpt-oss-120b` | Gemini | Every AI-read rule keeps its source quote; rows whose rules page could not be read get conservative defaults, marked as such |

Nothing AI-generated is posted to a community. Tailoring is rule-based, not AI-written. When a creator's own content is AI-generated (`ai_generated: true`), the service adds a disclosure line wherever the destination requires one.
