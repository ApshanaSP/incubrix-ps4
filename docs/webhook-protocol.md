# Partner webhook protocol (connector `webhook`)

A partner site that wants IncuBrix content implements three endpoints under its own base URL. One partner is one catalogue row (`connector_key=webhook`, `config.base_url`); no code is written per partner.

## Signing

Every request from IncuBrix carries:

| Header | Value |
|---|---|
| `X-IncuBrix-Timestamp` | Unix seconds when the request was sent |
| `X-IncuBrix-Signature` | `sha256=` + hex HMAC-SHA256 of `"<timestamp>." + raw request body`, keyed with the shared signing secret |
| `Idempotency-Key` | On `POST` only. The partner must return the existing post for a key it has already seen |

The partner must reject a bad signature or a timestamp more than 5 minutes old with `401`. GET and DELETE requests sign an empty body.

## Endpoints

| Method and path | Request | Response |
|---|---|---|
| `POST {base}/incubrix/posts` | JSON: `idempotency_key, title, body, body_format, link, canonical_url, media[], tags[], ai_generated` | `201 {"id", "url", "status"}` |
| `GET {base}/incubrix/posts/{id}` | none | `200 {"id", "url", "status", "views"?, "clicks"?, ...}`; `404` when deleted |
| `DELETE {base}/incubrix/posts/{id}` | none | `200 {"deleted": true}` |
| `GET {base}/incubrix/posts?idempotency_key=K` | none | `200 {"posts": [{"id", "url", "status"}]}`, used to recover after a timeout |

## Errors the partner returns

| Status | Meaning | IncuBrix code |
|---|---|---|
| 401 | bad or stale signature | `CONNECTION_AUTH_EXPIRED` |
| 403 | the partner revoked IncuBrix's permission | `CONNECTION_AUTH_EXPIRED`; the connection is marked `revoked` and the destination paused |
| 422 / 400 | content refused | `PLATFORM_CONTENT_REJECTED` (the partner's message is shown for review) |
| 429 + `Retry-After` | slow down | `PLATFORM_RATE_LIMIT`, with back-off to the reset time |
| 5xx | unavailable | `PLATFORM_UNAVAILABLE`; IncuBrix stops calling after 5 consecutive failures |

A reference implementation is the `webhook` mock in `app/mocks/server.py`.
