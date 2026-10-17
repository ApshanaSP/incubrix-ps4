# Sources and licences

Everything in the default path, and everything it outputs, may be used commercially. The service ships no AGPL code. The AGPL programs below run only as separate local test or storage containers in `docker-compose.yml`; the service talks to them over HTTP and contains none of their code.

## Python libraries (shipped in the service image)

| Library | Version | Licence | Used for |
|---|---|---|---|
| FastAPI | 0.128.0 | MIT | HTTP API, OpenAPI 3.1 generation |
| Starlette | (with FastAPI) | BSD-3-Clause | ASGI toolkit, middleware |
| Uvicorn | 0.40.0 | BSD-3-Clause | ASGI server |
| Pydantic | 2.11.7 | MIT | request and contract models |
| HTTPX | 0.28.1 | BSD-3-Clause | outbound platform calls, in-process mock transport |
| PyYAML | 6.0.2 | MIT | connector manifests, openapi.yaml |
| python-multipart | 0.0.22 | Apache-2.0 | file uploads |
| python-dotenv | 1.1.1 | BSD-3-Clause | optional .env loading |
| cryptography | 45.0.5 | Apache-2.0 / BSD-3-Clause | Fernet encryption of stored credentials |
| boto3 / botocore | 1.43.108 | Apache-2.0 | S3-compatible storage and presigned links |
| Beautiful Soup 4 | 4.13.4 | MIT | rules-page and catalogue page text extraction |
| langdetect | 1.0.9 | Apache-2.0 | language detection during catalogue import |
| truststore (optional) | 0.10.4 | MIT | use the OS certificate store when `USE_SYSTEM_CERTS=true` |
| pytest (tests only) | 9.1.1 | MIT | test runner |

## Containers used by `docker compose up` (not part of the service code)

| Image | Licence | Role |
|---|---|---|
| python:3.11-slim | PSF and Debian package licences | base image of the service |
| dessalines/lemmy 0.19.5, dessalines/lemmy-ui 0.19.5 | AGPL-3.0 | local Lemmy test instance (separate process, reached over HTTP) |
| postgres:16-alpine | PostgreSQL Licence | Lemmy's database |
| ghost:5-alpine | MIT | local Ghost test instance |
| chrislusf/seaweedfs | Apache-2.0 | local S3-compatible storage for development (MinIO no longer publishes Docker Hub images). Production points `S3_ENDPOINT` at any S3 service, for example AWS S3. |

## AI models and APIs

| Model / API | Licence or terms | Where | Default? |
|---|---|---|---|
| Built-in heuristic adapter (`mock`) | this repository | rules capture, relevance, tests | yes, default |
| `openai/gpt-oss-120b` served by Groq | model: Apache-2.0; Groq API commercial terms | rules capture, relevance (`AI_PROVIDER=groq`) | opt-in |
| `openai/gpt-oss-20b` served by Groq | model: Apache-2.0; Groq API commercial terms | real-catalogue build, after the 120b model's free daily token limit was used up (`GROQ_MODEL`); the model used is recorded per row | opt-in |
| Gemini `gemini-3.1-flash-lite`, Google Gemini API | Google API terms (commercial use allowed on paid tier) | real-catalogue build (the model is recorded per row) | opt-in |
| Gemini Flash (`gemini-flash-latest`), Google Gemini API | Google API terms (commercial use allowed on paid tier) | same tasks (`AI_PROVIDER=gemini`) | opt-in |

All AI calls go through `app/ai/providers.py`, so a provider is changed by configuration only. AI-written outputs (rules proposals, relevance reasons) are marked in the result manifest, and rules proposals are not used until a person confirms them.

## Platform APIs used by the connectors

Discourse REST API, Lemmy HTTP API v3, Forem (Dev.to) API v1, Ghost Admin API v5, Hashnode GraphQL API, Slack Web API (`chat.postMessage`, `chat.getPermalink`, `chat.delete`, `conversations.history`, `reactions.get`, `auth.test`), Media RSS 1.5 (`http://search.yahoo.com/mrss/`). The partner webhook protocol is defined in `docs/webhook-protocol.md`.

## Data and media

- No third-party media is included or produced. Test posts are plain text written for this project.
- `catalogue/real_catalogue.csv` lists public communities: their names, public URLs and posting rules. These are facts, read from public rules pages where `robots.txt` allows. Each AI-read rule keeps its source quote in `catalogue/real_catalogue_evidence.json`. Nothing was posted to any of these communities.
- Mock platform behaviour in `app/mocks/server.py` was written for this project from the public API documentation of each platform.
- Catalogue discovery (`tools/discover_destinations.py`) reads public APIs that robots.txt allows: Lemmy instances' `/api/v3/community/list`, the PeerTube instance index at instances.joinpeertube.org and each instance's `/api/v1/config/about`. Only names, URLs, descriptions and published rules are stored, as facts about the communities.
- `tools/check_real_apis.py` reads public posts on meta.discourse.org, dev.to and lemmy.world (read-only) to check the connectors' field use.
