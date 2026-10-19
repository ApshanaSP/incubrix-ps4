# Adding a connector (about 10 minutes to a certified mock)

One connector serves every destination of its kind, so a new platform is one connector; each forum, community or
site on it is then one catalogue row.

## 1. Make it from the template

```bash
python -m tools.new_connector circle --name "Circle communities"
```

This writes three files and nothing else (the registry finds new connectors by itself):

| File | What it is |
|---|---|
| `app/connectors/circle.py` | the Appendix A methods (`publish`, `get_status`, `delete`, `fetch_metrics`) plus `certify_probe` and `find_existing` (used after a timeout so a retry never posts twice) |
| `app/connectors/manifests/circle.yaml` | the manifest: tier, auth, surfaces and limits, rate limits, capabilities, error map, `instance_param`; `mock: template_rest` makes the mock platforms answer for it |
| `tests/test_connector_circle.py` | certification on the mock, the contract round trip with the same key twice, an expired token, a timeout then retry, a renamed API field |

## 2. Run its tests (they pass at once)

```bash
python -m pytest tests/test_connector_circle.py
```

## 3. Certify it in the running service

```bash
curl -X POST localhost:8080/api/v1/connections -H "content-type: application/json" \
  -d '{"connector_key":"circle","credentials":{"api_key":"sim-key"}}'
curl -X POST localhost:8080/api/v1/destinations -H "content-type: application/json" \
  -d '{"url":"https://my-circle.mock","connector_key":"circle","name":"My Circle","config":{"base_url":"https://my-circle.mock"},"connection_id":"con_..."}'
curl -X POST localhost:8080/api/v1/destinations/dst_.../certify
```

Or in the dashboard: the new destination appears on the Destinations tab; press **Test it now**.

## 4. Point it at the real platform

1. In `circle.py`, change the paths, the request body and the field names to the real API. Read every field the
   code relies on with `require(data, "field")`: if the platform renames it, certification fails with
   `SCHEMA_DRIFT` and the destination is demoted instead of failing silently.
2. In the manifest, set the real limits (`title_max_chars`, `body_max_chars`, accepted `content_types`), the rate
   limit, and the platform's error codes in `error_map` (only the eight shared codes are allowed).
3. Change `auth` and `credentials` if the platform uses OAuth or a bot token.
4. For a mock that behaves like the real API, add a handler in `app/mocks/server.py` (see `webhook()` for a short
   one), register it in `HANDLERS`, and remove the `mock:` line from the manifest.

## What the service does for every connector, without connector code

Posting rules, tailoring, the idempotency ledger and timeout recovery, 429 back-off using the reset time (and
`PLATFORM_QUOTA_EXHAUSTED` when the answer says a daily quota is used up), the circuit breaker after 5 failures
(with one trial call after `BREAKER_COOLDOWN_S`), token expiry from the connection's `expires_at`, certification and
recertification, demotion, tickets, metrics refresh and the kill switch are all outside the connector. A connector only talks to its platform and returns results.
