# Certification pack

Built 2026-10-17 23:46 by `python -m tools.make_cert_pack` against http://127.0.0.1:8088.

Each run publishes a test post, confirms the live URL, reads its status (and metrics where the connector has them), deletes it and confirms the deletion. Simulated destinations post to the built-in mock platforms; real local Lemmy and Ghost runs are at the end.

| Connector | Destination | Result | Level | Run time | Run record | Screenshot |
|---|---|---|---|---|---|---|
| discourse | Certification pack: discourse | passed | L4 | 0.063 s | [run.json](discourse/run.json) | [card.png](discourse/card.png) |
| lemmy | Certification pack: lemmy | passed | L4 | 0.016 s | [run.json](lemmy/run.json) | [card.png](lemmy/card.png) |
| devto | Certification pack: devto | passed | L4 | 0.015 s | [run.json](devto/run.json) | [card.png](devto/card.png) |
| ghost | Certification pack: ghost | passed | L3 | 0.031 s | [run.json](ghost/run.json) | [card.png](ghost/card.png) |
| webhook | Certification pack: webhook | passed | L2 | 0.016 s | [run.json](webhook/run.json) | [card.png](webhook/card.png) |
| assisted | Certification pack: assisted | passed | L1 | 0.016 s | [run.json](assisted/run.json) | [card.png](assisted/card.png) |
| hashnode | Certification pack: hashnode | passed | L4 | 0.032 s | [run.json](hashnode/run.json) | [card.png](hashnode/card.png) |
| slack | Certification pack: slack | passed | L4 | 0.031 s | [run.json](slack/run.json) | [card.png](slack/card.png) |
| mrss | Certification pack: mrss | passed | L2 | 0.016 s | [run.json](mrss/run.json) | [card.png](mrss/card.png) |

## Real local instances (docker compose, 16 Oct 2026)

Run on a real Lemmy 0.19.5 and a real Ghost 5 started by `docker compose up`; the posts were deleted afterwards. Numbers: `outputs/metrics/local_instances.json`.

| Destination | Screenshot of the real post | Run record |
|---|---|---|
| Local Lemmy c/incubrix_test | [local_lemmy_post.jpg](../docs/evidence/local_lemmy_post.jpg) | [local_instances.json](../outputs/metrics/local_instances.json) |
| Local Ghost blog | [local_ghost_post.jpg](../docs/evidence/local_ghost_post.jpg) | [local_instances.json](../outputs/metrics/local_instances.json) |
