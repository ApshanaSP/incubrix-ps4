"""IncuBrix Long-Tail Distribution service: HTTP API.

  /connector/v1/{connector_key}/{method}   the connector contract (Appendix A)
  /api/v1/...                              catalogue, publishing, certification, jobs
  /health, /ready                          liveness and readiness
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from fastapi import Body, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import certification, jobs, manifest, publisher, recommender, rules
from app.catalogue import importer, intel, store
from app.config import get_settings
from app.connectors import registry
from app.connectors.assisted import mark_posted
from app.connectors.base import Connection, Destination, Rendition, empty_metrics
from app.connectors.mrss import render_feed
from app.db import get_db, new_id, now_iso
from app.errors import ALL_CODES, ApiError, ConnectorError
from app.logs import log, setup_logging
from app.middleware import Idempotency, RequestContext
from app.storage import LocalStorage, get_storage, verify_link
from app.tailoring import ContentItem, tailor, tailor_many

logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_level)
    get_db()
    jobs.mark_interrupted()
    get_storage()
    background: list[asyncio.Task] = []
    if s.simulation_mode or _env_true("SEED_DEMO"):
        from app.seed import seed_demo

        created = seed_demo()
        if created and _env_true("CERTIFY_ON_START", True):
            background.append(asyncio.create_task(_certify_many(created)))
    if s.scheduler_enabled:
        background.append(asyncio.create_task(certification.scheduler_loop()))
    log(logger, logging.INFO, "service started", simulation=s.simulation_mode, ai_provider=s.ai_provider,
        storage=type(get_storage()).__name__)
    yield
    for t in background:
        t.cancel()


def _env_true(name: str, default: bool = False) -> bool:
    import os

    v = os.getenv(name)
    return default if v is None else v.lower() in ("1", "true", "yes")


async def _certify_many(ids: list[str]) -> list[dict[str, Any]]:
    out = []
    for dest_id in ids:
        try:
            out.append(await certification.certify(dest_id))
        except Exception as exc:  # noqa: BLE001
            logger.exception("certification crashed")
            out.append({"destination_id": dest_id, "passed": False, "error": str(exc)})
    return out


app = FastAPI(
    title="IncuBrix Long-Tail Distribution",
    version=manifest.SERVICE_VERSION,
    description="Connectors, posting rules, certification, catalogue tools, tailoring and recommendations for "
                "long-tail destinations. Errors are always {\"error\": {code, message, retryable, details}}.",
    lifespan=lifespan,
)
app.add_middleware(Idempotency)
app.add_middleware(RequestContext)


def _openapi_with_auth() -> dict[str, Any]:
    """The spec declares the optional bearer token, so /docs shows an Authorize button on the live demo."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "bearer": {"type": "http", "scheme": "bearer",
                   "description": "Needed only when API_TOKEN is set (the live demo); local runs need no token."}}
    schema["security"] = [{}, {"bearer": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_auth  # type: ignore[method-assign]


# ---------------------------------------------------------------- errors
@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.http_status or 500)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(ApiError("VALIDATION_ERROR", "request is invalid", details={"fields": errs}).to_dict(),
                        status_code=400)


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR" if exc.status_code < 500 else "INTERNAL_ERROR"
    return JSONResponse(ApiError(code, str(exc.detail), http_status=exc.status_code).to_dict(), status_code=exc.status_code)


@app.exception_handler(Exception)
async def crash_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(ApiError("INTERNAL_ERROR", f"{type(exc).__name__}: unexpected error").to_dict(), status_code=500)


ERRORS = {"default": {"description": "Error", "content": {"application/json": {"example": {
    "error": {"code": "NOT_FOUND", "message": "destination dst_x not found", "retryable": False, "details": {}}}}}}}


# ---------------------------------------------------------------- models
class JobAccepted(BaseModel):
    job_id: str
    kind: str
    status: str
    progress: int
    result: Any = None
    error: Any = None
    links: dict[str, str]
    created_at: str
    updated_at: str


class DestinationIn(BaseModel):
    name: str | None = None
    url: str
    connector_key: str
    platform_key: str | None = None
    surface: str | None = None
    category: str | None = None
    audience_tags: list[str] = Field(default_factory=list)
    language: str | None = None
    geo: str | None = None
    est_audience: int | None = None
    rules_url: str | None = None
    frequency_cap: str | None = None
    self_promo_allowed: str | None = None
    link_policy: str | None = None
    disclosure_required: bool | None = None
    automation_allowed: bool | None = None
    relevance_score: int | None = Field(None, ge=1, le=5)
    relevance_reason: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None


class DestinationPatch(BaseModel):
    name: str | None = None
    audience_tags: list[str] | None = None
    language: str | None = None
    rules_url: str | None = None
    frequency_cap: str | None = None
    self_promo_allowed: str | None = None
    link_policy: str | None = None
    disclosure_required: bool | None = None
    automation_allowed: bool | None = None
    relevance_score: int | None = Field(None, ge=1, le=5)
    relevance_reason: str | None = None
    config: dict[str, Any] | None = None
    connection_id: str | None = None
    status: Literal["uncertified", "certified", "demoted", "paused", "excluded"] | None = None


class ConnectionIn(BaseModel):
    connector_key: str
    name: str | None = None
    credentials: dict[str, Any] = Field(default_factory=dict, description="Inline secrets (stored encrypted) or "
                                        "references such as {\"api_key_ref\": \"env:DISCOURSE_API_KEY\"}")
    base_url: str | None = None
    expires_at: str | None = None


class ConnectionPatch(BaseModel):
    credentials: dict[str, Any]


class PublicationIn(BaseModel):
    content: ContentItem
    destination_ids: list[str] | None = Field(None, description="Where to post; omit and set recommend_top_k to let "
                                                               "the recommender choose")
    recommend_top_k: int | None = Field(None, ge=1, le=50)
    dry_run: bool = Field(False, description="Tailor and check rules only; nothing is posted")
    callback_url: str | None = None


class TailorIn(BaseModel):
    content: ContentItem
    destination_ids: list[str]


class RecommendIn(BaseModel):
    content: ContentItem
    top_k: int = Field(10, ge=1, le=100)
    include_uncertified: bool = False


class CertifyIn(BaseModel):
    destination_ids: list[str] | None = None
    all: bool = False
    callback_url: str | None = None


class RulesCaptureIn(BaseModel):
    rules_url: str | None = None
    rules_text: str | None = Field(None, description="Rules pasted by a person when the page cannot be fetched "
                                                     "(robots.txt, login wall)")
    provider: Literal["mock", "groq", "gemini"] | None = None
    callback_url: str | None = None


class RulesDecisionIn(BaseModel):
    decided_by: str = Field(..., description="Who reviewed the proposal")
    overrides: dict[str, Any] | None = None
    reject: bool = False


class RelevanceIn(BaseModel):
    destination_ids: list[str] | None = None
    provider: Literal["mock", "groq", "gemini"] | None = None
    callback_url: str | None = None


class ImportUrlIn(BaseModel):
    url: str
    score_with_ai: bool = False
    commit: bool = True
    callback_url: str | None = None


class MetricsIn(BaseModel):
    post_ids: list[str] | None = None
    since: str | None = None
    callback_url: str | None = None


class KillSwitchIn(BaseModel):
    disabled: bool
    reason: str | None = None


class AssistPostedIn(BaseModel):
    live_url: str


class SimFaultIn(BaseModel):
    destination_id: str | None = Field(None, description="Use this destination's mock instance")
    platform: str | None = None
    host: str | None = None
    kind: Literal["timeout_after_create", "timeout", "rate_limit", "auth_expired", "reject", "rename_field",
                  "revoke", "server_error"]
    op: Literal["publish", "status", "delete", "list", "any"] = "publish"
    count: int = 1
    retry_after: float = 2


class ContractCall(BaseModel):
    """Body of POST /connector/v1/{connector_key}/{method}. Which fields are needed depends on the method.
    `destination` and `connection` may be full objects or {"id": "..."} references to stored ones."""

    rendition: Rendition | None = None
    destination: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    idempotency_key: str | None = None
    platform_post_id: str | None = None
    since: str | None = None


# ---------------------------------------------------------------- health
@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
async def ready() -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        get_db().one("SELECT 1 AS ok")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
    try:
        storage = get_storage()
        storage.put_text("_ready/probe.txt", now_iso())
        checks["storage"] = f"ok ({type(storage).__name__})"
    except Exception as exc:  # noqa: BLE001
        checks["storage"] = f"error: {type(exc).__name__}"
    checks["connectors"] = len(registry.keys())
    checks["simulation_mode"] = get_settings().simulation_mode
    ok = all(not str(v).startswith("error") for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


UI_PAGE = Path(__file__).parent / "static" / "dashboard.html"


@app.get("/", include_in_schema=False)
async def root(request: Request) -> Any:
    """Browsers get the dashboard; API clients get the service links as JSON."""
    if "text/html" in request.headers.get("accept", ""):
        # always the latest page after an update (browsers otherwise keep showing a cached copy)
        return FileResponse(UI_PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return {"service": "IncuBrix Long-Tail Distribution", "dashboard": "/", "docs": "/docs",
            "openapi": "/openapi.yaml", "health": "/health", "ready": "/ready"}


def _scenario_dir() -> Path:
    return get_settings().batch_inputs_dir


@app.get("/ui/scenarios", include_in_schema=False)
async def ui_scenarios() -> dict[str, Any]:
    """The batch runner's inputs, so the dashboard runs exactly the tested hard cases."""
    out = []
    for f in sorted(_scenario_dir().glob("*.json")):
        if f.name.startswith("_"):
            continue
        case = json.loads(f.read_text(encoding="utf-8"))
        out.append({"name": case.get("name") or f.stem, "description": case.get("description", ""),
                    "steps": len(case.get("steps", []))})
    return {"scenarios": out}


@app.get("/ui/scenarios/{name}", include_in_schema=False)
async def ui_scenario(name: str) -> dict[str, Any]:
    f = _scenario_dir() / f"{name}.json"
    if "/" in name or "\\" in name or ".." in name or not f.is_file():
        raise ApiError("NOT_FOUND", f"scenario {name} not found")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/ui/files/{name}", include_in_schema=False)
async def ui_file(name: str) -> Response:
    f = _scenario_dir() / "files" / name
    if "/" in name or "\\" in name or ".." in name or not f.is_file():
        raise ApiError("NOT_FOUND", f"file {name} not found")
    return FileResponse(f)


@app.get("/openapi.yaml", include_in_schema=False)
async def openapi_yaml() -> PlainTextResponse:
    return PlainTextResponse(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True),
                             media_type="application/yaml")


# ---------------------------------------------------------------- connector contract
CONTRACT_METHODS = ["validate", "publish", "get_status", "delete", "fetch_metrics", "refresh_auth", "build_assist_pack"]


def _contract_destination(raw: dict[str, Any] | None, connector_key: str) -> tuple[Destination, dict[str, Any]]:
    if not raw:
        raise ApiError("VALIDATION_ERROR", "destination is required", details={"field": "destination"})
    if raw.get("id") and len(raw) <= 2 and get_db().one("SELECT id FROM destinations WHERE id=?", [raw["id"]]):
        row = store.get_destination(raw["id"])
        return store.to_model(row), row
    dest = Destination(**{**raw, "connector_key": raw.get("connector_key") or connector_key})
    if not dest.id:
        import hashlib

        dest.id = "inline_" + hashlib.sha256(json.dumps(dest.config, sort_keys=True).encode()
                                             + (dest.url or "").encode()).hexdigest()[:12]
    row = get_db().one("SELECT * FROM destinations WHERE id=?", [dest.id])
    if row is None:
        row = {**dest.model_dump(), "status": "certified", "rules_status": "confirmed", "connection_id": None}
    return dest, row


def _contract_connection(raw: dict[str, Any] | None, connector_key: str) -> Connection:
    if not raw:
        return Connection(connector_key=connector_key)
    if raw.get("id") and not raw.get("credentials"):
        return store.load_connection(raw["id"], connector_key)
    return Connection(**{**raw, "connector_key": connector_key})


@app.post("/connector/v1/{connector_key}/{method}", tags=["connector contract"], responses=ERRORS)
async def contract(connector_key: str, method: str, call: ContractCall) -> dict[str, Any]:
    """The IncuBrix connector contract. Methods: validate, publish, get_status, delete, fetch_metrics,
    refresh_auth, build_assist_pack. Errors use the shared codes from Appendix A."""
    if method not in CONTRACT_METHODS:
        raise ApiError("UNSUPPORTED_METHOD", f"unknown method '{method}'", details={"methods": CONTRACT_METHODS})
    connector = registry.get(connector_key)
    if method not in ("validate", "publish") and not connector.implements(method):
        raise ApiError("UNSUPPORTED_METHOD", f"{connector_key} does not implement {method}",
                       details={"capabilities": connector.capabilities})
    if method == "validate":
        dest, row = _contract_destination(call.destination, connector_key)
        conn = _contract_connection(call.connection, connector_key)
        rendition = call.rendition or Rendition()
        spec = await connector.validate(rendition, dest, conn)
        rule = rules.check(rendition, dest, native=connector_key != "assisted", rules_status=row.get("rules_status"),
                           use_ledger=bool(get_db().one("SELECT id FROM destinations WHERE id=?", [dest.id])))
        violations = spec["violations"] + rule["violations"]
        return {"ok": not violations, "violations": violations, "warnings": rule["warnings"]}
    if method == "publish":
        if not call.idempotency_key:
            raise ApiError("VALIDATION_ERROR", "idempotency_key is required", details={"field": "idempotency_key"})
        dest, row = _contract_destination(call.destination, connector_key)
        conn = _contract_connection(call.connection, connector_key) if call.connection else None
        res = await publisher.publish(call.rendition or Rendition(), row, call.idempotency_key, connection=conn,
                                      skip_cert_gate=True)
        if res["state"] in ("blocked", "rescheduled"):
            code = res.get("code", "DEST_RULE_VIOLATION")
            raise ApiError(code, res.get("reason") or "blocked by a posting rule",
                           details={"violations": res.get("violations", []), "reschedule_at": res.get("reschedule_at")})
        out = {"platform_post_id": res["platform_post_id"], "live_url": res["live_url"],
               "status": res.get("status") or ("live" if res["state"] == "published" else res["state"])}
        if res.get("assist_pack"):
            out["assist_pack"] = res["assist_pack"]
        if res.get("replayed"):
            out["replayed"] = True
        return out
    if method == "build_assist_pack":
        dest, _ = _contract_destination(call.destination, connector_key)
        return await connector.build_assist_pack(call.rendition or Rendition(), dest)
    conn = _contract_connection(call.connection, connector_key)
    if method == "refresh_auth":
        return await connector.refresh_auth(conn)
    if not call.platform_post_id:
        raise ApiError("VALIDATION_ERROR", "platform_post_id is required", details={"field": "platform_post_id"})
    if method == "get_status":
        return await connector.get_status(call.platform_post_id, conn)
    if method == "delete":
        return await connector.delete(call.platform_post_id, conn)
    return await connector.fetch_metrics(call.platform_post_id, conn, call.since)


@app.post("/api/v1/connector/{connector_key}/{method}", tags=["connector contract"], responses=ERRORS)
async def contract_alias(connector_key: str, method: str, call: ContractCall) -> dict[str, Any]:
    """Same as /connector/v1/{connector_key}/{method}, under /api/v1/."""
    return await contract(connector_key, method, call)


# ---------------------------------------------------------------- connectors
@app.get("/api/v1/connectors", tags=["connectors"])
async def list_connectors() -> dict[str, Any]:
    return {"connectors": registry.manifests()}


@app.get("/api/v1/connectors/{connector_key}", tags=["connectors"], responses=ERRORS)
async def get_connector(connector_key: str) -> dict[str, Any]:
    return next(m for m in registry.manifests() if m["key"] == registry.get(connector_key, check_kill_switch=False).key)


@app.post("/api/v1/connectors/{connector_key}/kill-switch", tags=["connectors"], responses=ERRORS)
async def kill_switch(connector_key: str, body: KillSwitchIn) -> dict[str, Any]:
    registry.set_kill_switch(connector_key, body.disabled, body.reason)
    return {"connector_key": connector_key, "disabled": body.disabled, "reason": body.reason}


@app.get("/api/v1/errors", tags=["connectors"])
async def list_errors() -> dict[str, Any]:
    return {"errors": [{"code": c, "http_status": s, "retry": r, "meaning": m} for c, (s, r, m) in ALL_CODES.items()]}


# ---------------------------------------------------------------- connections
@app.post("/api/v1/connections", tags=["connections"], status_code=201, responses=ERRORS)
async def create_connection(body: ConnectionIn) -> dict[str, Any]:
    return store.create_connection(body.connector_key, body.credentials, body.name, body.base_url, body.expires_at)


@app.get("/api/v1/connections", tags=["connections"])
async def list_connections() -> dict[str, Any]:
    return {"connections": [store.public_connection(r) for r in get_db().all("SELECT * FROM connections ORDER BY created_at")]}


@app.get("/api/v1/connections/{connection_id}", tags=["connections"], responses=ERRORS)
async def get_connection(connection_id: str) -> dict[str, Any]:
    return store.public_connection(store.get_connection_row(connection_id))


@app.patch("/api/v1/connections/{connection_id}", tags=["connections"], responses=ERRORS)
async def reconnect(connection_id: str, body: ConnectionPatch) -> dict[str, Any]:
    """Replace credentials (reconnect after an expired or revoked token). Paused destinations resume."""
    return store.update_connection_credentials(connection_id, body.credentials)


# ---------------------------------------------------------------- destinations
@app.get("/api/v1/destinations", tags=["catalogue"])
async def list_destinations(connector_key: str | None = None, status: str | None = None,
                            language: str | None = None) -> dict[str, Any]:
    rows = store.list_destinations(connector_key=connector_key, status=status, language=language)
    return {"destinations": [store.public(r) for r in rows], "count": len(rows)}


@app.post("/api/v1/destinations", tags=["catalogue"], status_code=201, responses=ERRORS)
async def add_destination(body: DestinationIn) -> dict[str, Any]:
    """Add one destination: one catalogue row, no code. It must be certified before it is used."""
    row, created = store.upsert_destination(body.model_dump(exclude_none=True))
    return {**store.public(row), "created": created}


@app.get("/api/v1/destinations/{destination_id}", tags=["catalogue"], responses=ERRORS)
async def get_destination(destination_id: str) -> dict[str, Any]:
    row = store.get_destination(destination_id)
    certs = get_db().all("SELECT id, status, finished_at, duration_s, evidence_url FROM certifications "
                         "WHERE destination_id=? ORDER BY finished_at DESC LIMIT 5", [destination_id])
    return {**store.public(row), "recent_certifications": certs}


@app.patch("/api/v1/destinations/{destination_id}", tags=["catalogue"], responses=ERRORS)
async def patch_destination(destination_id: str, body: DestinationPatch) -> dict[str, Any]:
    return store.public(store.update_destination(destination_id, body.model_dump(exclude_none=True)))


@app.post("/api/v1/destinations/{destination_id}/certify", tags=["certification"], status_code=202,
          response_model=JobAccepted, responses=ERRORS)
async def certify_one(destination_id: str, callback_url: str | None = Body(None, embed=True)) -> dict[str, Any]:
    store.get_destination(destination_id)

    async def run(progress):
        res = await certification.certify(destination_id)
        return {**res, "links": {"evidence": res["evidence_url"]},
                "manifest": manifest.build("certification", connectors=[store.get_destination(destination_id)["connector_key"]])}

    return jobs.submit("certification", {"destination_id": destination_id}, run, callback_url)


@app.post("/api/v1/certifications", tags=["certification"], status_code=202, response_model=JobAccepted, responses=ERRORS)
async def certify_many(body: CertifyIn) -> dict[str, Any]:
    ids = body.destination_ids or ([r["id"] for r in store.list_destinations()] if body.all else [])
    if not ids:
        raise ApiError("VALIDATION_ERROR", "give destination_ids or all=true")
    for i in ids:
        store.get_destination(i)

    async def run(progress):
        timer = manifest.Timer()
        results = []
        for n, i in enumerate(ids):
            results.append(await certification.certify(i))
            progress(100 * (n + 1) / len(ids))
        summary = {"certified": sum(r["passed"] for r in results), "failed": sum(not r["passed"] for r in results)}
        link = get_storage().put_json(f"certifications/batch-{uuid.uuid4().hex[:8]}.json", results)
        return {"summary": summary, "results": results, "links": {"results": link},
                "manifest": manifest.build("certification_batch", timer=timer,
                                           connectors=sorted({store.get_destination(i)["connector_key"] for i in ids}),
                                           units=len(ids))}

    return jobs.submit("certification_batch", body.model_dump(), run, body.callback_url)


@app.get("/api/v1/certifications", tags=["certification"])
async def list_certifications(destination_id: str | None = None, limit: int = Query(50, le=500)) -> dict[str, Any]:
    sql = "SELECT * FROM certifications"
    params: list[Any] = []
    if destination_id:
        sql += " WHERE destination_id=?"
        params.append(destination_id)
    rows = get_db().all(sql + " ORDER BY started_at DESC LIMIT ?", [*params, limit])
    return {"certifications": rows}


@app.post("/api/v1/recertification/run", tags=["certification"], status_code=202, response_model=JobAccepted)
async def run_recertification(callback_url: str | None = Body(None, embed=True)) -> dict[str, Any]:
    """Re-test every certified destination last certified more than RECERT_AFTER_DAYS ago (also runs on a schedule)."""
    async def run(progress):
        res = await certification.recertify_due()
        return {**res, "manifest": manifest.build("recertification")}

    return jobs.submit("recertification", {}, run, callback_url)


# ---------------------------------------------------------------- rules capture
@app.post("/api/v1/destinations/{destination_id}/rules/capture", tags=["rules"], status_code=202,
          response_model=JobAccepted, responses=ERRORS)
async def capture_rules(destination_id: str, body: RulesCaptureIn) -> dict[str, Any]:
    """An AI step reads the community's rules page and proposes posting rules. They are NOT used until a person
    confirms them (POST .../rules/{proposal_id}/confirm); until then posting there is blocked."""
    store.get_destination(destination_id)

    async def run(progress):
        from app.ai.providers import get_provider

        provider = get_provider(body.provider)
        timer = manifest.Timer()
        res = await intel.capture_rules(destination_id, provider, body.rules_url, body.rules_text)
        res["manifest"] = manifest.build("rules_capture", timer=timer, ai_usage=[provider.usage.as_dict()],
                                         ai_generated=provider.name != "mock", units=1)
        res["links"] = {"proposal": get_storage().put_json(f"rules/{destination_id}/{res['proposal_id']}.json", res)}
        return res

    return jobs.submit("rules_capture", {"destination_id": destination_id, **body.model_dump()}, run, body.callback_url)


@app.get("/api/v1/destinations/{destination_id}/rules/proposals", tags=["rules"], responses=ERRORS)
async def list_rule_proposals(destination_id: str) -> dict[str, Any]:
    return {"proposals": get_db().all("SELECT * FROM rule_proposals WHERE destination_id=? ORDER BY created_at DESC",
                                      [destination_id])}


@app.post("/api/v1/destinations/{destination_id}/rules/{proposal_id}/confirm", tags=["rules"], responses=ERRORS)
async def confirm_rules(destination_id: str, proposal_id: str, body: RulesDecisionIn) -> dict[str, Any]:
    """A person confirms (optionally correcting with overrides) or rejects the AI-proposed rules."""
    return intel.confirm_rules(destination_id, proposal_id, body.decided_by, body.overrides, body.reject)


# ---------------------------------------------------------------- relevance
@app.post("/api/v1/relevance", tags=["catalogue"], status_code=202, response_model=JobAccepted, responses=ERRORS)
async def relevance(body: RelevanceIn) -> dict[str, Any]:
    """Score destinations 1-5 against IncuBrix's audience with a short reason each."""
    ids = body.destination_ids or [r["id"] for r in store.list_destinations()]

    async def run(progress):
        from app.ai.providers import get_provider

        provider = get_provider(body.provider)
        timer = manifest.Timer()
        results = []
        for n, i in enumerate(ids):
            row = store.get_destination(i)
            res = await intel.score_relevance(row, provider)
            changes = {"relevance_score": res["score"], "relevance_reason": res["reason"]}
            if not row.get("audience_tags") and res["audience_tags"]:
                changes["audience_tags"] = res["audience_tags"]
            store.update_destination(i, changes)
            results.append({"destination_id": i, "name": row["name"], **res})
            progress(100 * (n + 1) / len(ids))
        link = get_storage().put_json(f"relevance/{uuid.uuid4().hex[:8]}.json", results)
        return {"results": results, "links": {"results": link},
                "manifest": manifest.build("relevance", timer=timer, ai_usage=[provider.usage.as_dict()],
                                           ai_generated=provider.name != "mock", units=len(ids))}

    return jobs.submit("relevance", body.model_dump(), run, body.callback_url)


# ---------------------------------------------------------------- catalogue import / export
async def _import_job(data: bytes, source: str, score_with_ai: bool, commit: bool, callback_url: str | None) -> dict[str, Any]:
    stored = get_storage().put_bytes(f"imports/inputs/{uuid.uuid4().hex[:8]}.csv", data, "text/csv")

    async def run(progress):
        timer = manifest.Timer()
        res = await importer.import_catalogue(data, source=source, progress=progress, score_with_ai=score_with_ai,
                                              commit=commit)
        res["links"]["input"] = stored
        res["manifest"] = manifest.build("catalogue_import", timer=timer, units=res["summary"]["rows"],
                                         settings_used={"score_with_ai": score_with_ai, "commit": commit,
                                                        "reachability_mode": get_settings().reachability_mode})
        return res

    return jobs.submit("catalogue_import", {"source": source, "score_with_ai": score_with_ai, "commit": commit},
                       run, callback_url)


@app.post("/api/v1/catalogue/imports", tags=["catalogue"], status_code=202, response_model=JobAccepted, responses=ERRORS)
async def import_from_url(body: ImportUrlIn) -> dict[str, Any]:
    """Import a destinations CSV from a URL. Validates, de-duplicates (normalised URL + platform), checks
    reachability, enriches, scores relevance and writes a row-by-row report."""
    url, headers = body.url, {}
    s = get_settings()
    public, internal = urlsplit(s.s3_public_endpoint or ""), urlsplit(s.s3_endpoint or "")
    if public.netloc and internal.netloc and public.netloc != internal.netloc and urlsplit(url).netloc == public.netloc:
        # one of our own storage links (from POST /api/v1/uploads): inside compose the public host
        # (localhost:8333) is not this container, so connect to the internal host and keep the signed Host header
        url = url.replace(f"{public.scheme}://{public.netloc}", f"{internal.scheme}://{internal.netloc}", 1)
        headers["Host"] = public.netloc
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ApiError("UPSTREAM_FETCH_FAILED", f"could not download the CSV: {type(exc).__name__}") from exc
    return await _import_job(resp.content, body.url, body.score_with_ai, body.commit, body.callback_url)


@app.post("/api/v1/catalogue/imports/upload", tags=["catalogue"], status_code=202, response_model=JobAccepted,
          responses=ERRORS)
async def import_upload(file: UploadFile = File(...), score_with_ai: bool = Form(False), commit: bool = Form(True),
                        callback_url: str | None = Form(None)) -> dict[str, Any]:
    """Same as /catalogue/imports, with the CSV uploaded as multipart form data."""
    data = await file.read()
    return await _import_job(data, file.filename or "upload.csv", score_with_ai, commit, callback_url)


@app.get("/api/v1/catalogue/export.csv", tags=["catalogue"])
async def export_csv() -> Response:
    return Response(importer.export_catalogue_csv(), media_type="text/csv")


# ---------------------------------------------------------------- tailoring / recommendations
@app.post("/api/v1/tailor", tags=["publishing"], responses=ERRORS)
async def tailor_endpoint(body: TailorIn) -> dict[str, Any]:
    """Show the rendition each destination would get, with notes on every change. Nothing is posted."""
    dests = [store.to_model(store.get_destination(i)) for i in body.destination_ids]
    timer = manifest.Timer()
    out = tailor_many(body.content, dests)
    for item, d in zip(out, dests):
        r = Rendition(**item["rendition"])
        spec = await registry.get(d.connector_key, check_kill_switch=False).validate(r, d, None)
        chk = rules.check(r, d, native=d.connector_key != "assisted",
                          rules_status=store.get_destination(d.id)["rules_status"])
        item["would_post"] = spec["ok"] and chk["ok"]
        item["violations"] = spec["violations"] + chk["violations"]
        item["warnings"] = chk["warnings"]
    return {"renditions": out, "manifest": manifest.build("tailor", timer=timer,
                                                          connectors=sorted({d.connector_key for d in dests}))}


@app.post("/api/v1/recommendations", tags=["publishing"], responses=ERRORS)
async def recommend(body: RecommendIn) -> dict[str, Any]:
    """Rank destinations for one content item by fit (audience, rules, past results, format) with reasons."""
    timer = manifest.Timer()
    res = recommender.recommend(body.content, body.top_k, body.include_uncertified)
    res["manifest"] = manifest.build("recommendation", timer=timer)
    return res


# ---------------------------------------------------------------- publications
@app.post("/api/v1/publications", tags=["publishing"], status_code=202, response_model=JobAccepted, responses=ERRORS)
async def create_publication(body: PublicationIn, idempotency_key: str | None = Header(None)) -> dict[str, Any]:
    """Tailor the content for each destination, check format limits and posting rules, then publish.
    Each destination's post uses the key '<Idempotency-Key>:<destination_id>', so retrying never posts twice."""
    if body.destination_ids:
        dest_ids = body.destination_ids
    elif body.recommend_top_k:
        dest_ids = [r["destination_id"] for r in recommender.recommend(body.content, body.recommend_top_k)["recommendations"]]
    else:
        raise ApiError("VALIDATION_ERROR", "give destination_ids or recommend_top_k")
    for i in dest_ids:
        store.get_destination(i)
    publication_id = new_id("pub")
    base_key = idempotency_key or publication_id

    async def run(progress):
        timer = manifest.Timer()
        results = []
        warnings: list[str] = []
        if not idempotency_key:
            warnings.append("no Idempotency-Key header: a retry of this request would be a new publication")
        for n, dest_id in enumerate(dest_ids):
            row = store.get_destination(dest_id)
            dest = store.to_model(row)
            rendition, notes = tailor(body.content, dest)
            entry: dict[str, Any] = {"destination_id": dest_id, "name": dest.name, "connector_key": dest.connector_key,
                                     "tailoring_notes": notes, "rendition": rendition.model_dump(exclude_none=True)}
            if body.dry_run:
                spec = await registry.get(dest.connector_key, check_kill_switch=False).validate(rendition, dest, None)
                chk = rules.check(rendition, dest, native=dest.connector_key != "assisted",
                                  rules_status=row.get("rules_status"))
                entry.update(state="dry_run", would_post=spec["ok"] and chk["ok"] and row["status"] == "certified",
                             violations=spec["violations"] + chk["violations"], warnings=chk["warnings"])
            else:
                try:
                    entry.update(await publisher.publish(rendition, row, f"{base_key}:{dest_id}",
                                                         publication_id=publication_id))
                except ApiError as exc:
                    err = exc.to_dict()["error"]
                    key = f"{base_key}:{dest_id}"
                    post = get_db().one("SELECT id, state, reschedule_at FROM posts WHERE idempotency_key=?", [key])
                    entry.update(state="failed", error=err, idempotency_key=key,
                                 post_id=post["id"] if post else None,
                                 post_state=post["state"] if post else None,
                                 reschedule_at=post["reschedule_at"] if post else None)
            results.append(entry)
            progress(100 * (n + 1) / len(dest_ids))
        timer.mark("published")
        summary: dict[str, int] = {}
        for r in results:
            summary[r["state"]] = summary.get(r["state"], 0) + 1
        out = {"publication_id": publication_id, "summary": summary, "results": results}
        out["manifest"] = manifest.build("publication", timer=timer, warnings=warnings,
                                         connectors=sorted({r["connector_key"] for r in results}),
                                         settings_used={"dry_run": body.dry_run}, units=len(results),
                                         ai_generated=body.content.ai_generated)
        out["links"] = {"result": get_storage().put_json(f"publications/{publication_id}.json", out)}
        return out

    return jobs.submit("publication", {"publication_id": publication_id, "destinations": dest_ids,
                                       "dry_run": body.dry_run}, run, body.callback_url)


@app.get("/api/v1/posts", tags=["publishing"])
async def list_posts(destination_id: str | None = None, state: str | None = None, include_tests: bool = False,
                     limit: int = Query(100, le=1000)) -> dict[str, Any]:
    sql = "SELECT id, destination_id, connector_key, state, platform_post_id, live_url, is_test, promotional, title, " \
          "reason, reschedule_at, idempotency_key, attempts, created_at, published_at, deleted_at FROM posts WHERE 1=1"
    params: list[Any] = []
    if destination_id:
        sql += " AND destination_id=?"
        params.append(destination_id)
    if state:
        sql += " AND state=?"
        params.append(state)
    if not include_tests:
        sql += " AND is_test=0"
    return {"posts": get_db().all(sql + " ORDER BY created_at DESC LIMIT ?", [*params, limit])}


@app.get("/api/v1/posts/{post_id}", tags=["publishing"], responses=ERRORS)
async def get_post(post_id: str) -> dict[str, Any]:
    row = get_db().one("SELECT * FROM posts WHERE id=?", [post_id])
    if not row:
        raise ApiError("NOT_FOUND", f"post {post_id} not found")
    row["metrics"] = get_db().all("SELECT fetched_at, data FROM metrics WHERE post_id=? ORDER BY fetched_at DESC LIMIT 5",
                                  [post_id])
    return row


@app.post("/api/v1/posts/{post_id}/delete", tags=["publishing"], status_code=202, response_model=JobAccepted,
          responses=ERRORS)
async def delete_post(post_id: str, callback_url: str | None = Body(None, embed=True)) -> dict[str, Any]:
    if not get_db().one("SELECT id FROM posts WHERE id=?", [post_id]):
        raise ApiError("NOT_FOUND", f"post {post_id} not found")

    async def run(progress):
        res = await publisher.delete_post(post_id)
        return {**res, "manifest": manifest.build("delete")}

    return jobs.submit("delete", {"post_id": post_id}, run, callback_url)


@app.post("/api/v1/posts/{post_id}/retry", tags=["publishing"], status_code=202, response_model=JobAccepted,
          responses=ERRORS)
async def retry_post(post_id: str, callback_url: str | None = Body(None, embed=True)) -> dict[str, Any]:
    """Retry one post with its original idempotency key. If an earlier attempt timed out, the platform is checked
    first, so the retry returns the existing post instead of creating a second one."""
    post = get_db().one("SELECT * FROM posts WHERE id=?", [post_id])
    if not post:
        raise ApiError("NOT_FOUND", f"post {post_id} not found")

    async def run(progress):
        row = store.get_destination(post["destination_id"])
        try:
            res = await publisher.publish(Rendition(**(post["rendition"] or {})), row, post["idempotency_key"],
                                          publication_id=post.get("publication_id"))
        except ApiError as exc:
            res = {"post_id": post_id, "state": "failed", "error": exc.to_dict()["error"]}
        return {**res, "manifest": manifest.build("retry", connectors=[post["connector_key"]])}

    return jobs.submit("retry", {"post_id": post_id}, run, callback_url)


@app.post("/api/v1/assist/{pack_id}/posted", tags=["publishing"], responses=ERRORS)
async def assist_posted(pack_id: str, body: AssistPostedIn) -> dict[str, Any]:
    """A person posted an assisted pack by hand: record its live URL."""
    state = mark_posted(pack_id, body.live_url)
    get_db().execute("UPDATE posts SET state='published', live_url=?, published_at=? WHERE platform_post_id=?",
                     [body.live_url, now_iso(), f"assist:{pack_id}"])
    return state


# ---------------------------------------------------------------- metrics
@app.post("/api/v1/metrics/refresh", tags=["metrics"], status_code=202, response_model=JobAccepted)
async def refresh_metrics(body: MetricsIn) -> dict[str, Any]:
    """fetch_metrics for published posts, normalised to views, likes, comments, shares, saves, watch_time_s, clicks."""
    async def run(progress):
        if body.post_ids:
            posts = [get_db().one("SELECT * FROM posts WHERE id=?", [i]) for i in body.post_ids]
            posts = [p for p in posts if p]
        else:
            posts = get_db().all("SELECT * FROM posts WHERE state='published' AND is_test=0")
        results = []
        for n, p in enumerate(posts):
            connector = registry.get(p["connector_key"])
            if not connector.implements("fetch_metrics"):
                results.append({"post_id": p["id"], "skipped": "connector has no metrics"})
                continue
            row = store.get_destination(p["destination_id"])
            conn = store.load_connection(row.get("connection_id"), p["connector_key"])
            try:
                data = await connector.fetch_metrics(p["platform_post_id"], conn, body.since)
                data = {**empty_metrics(), **data}
                get_db().insert("metrics", {"post_id": p["id"], "fetched_at": now_iso(), "data": data})
                results.append({"post_id": p["id"], "destination_id": p["destination_id"], "metrics": data})
            except ConnectorError as exc:
                results.append({"post_id": p["id"], "error": exc.to_dict()["error"]})
            progress(100 * (n + 1) / max(1, len(posts)))
        return {"results": results, "manifest": manifest.build("metrics", units=len(results))}

    return jobs.submit("metrics", body.model_dump(), run, body.callback_url)


# ---------------------------------------------------------------- jobs, tickets, files, feeds
@app.get("/api/v1/jobs/{job_id}", tags=["jobs"], response_model=JobAccepted, responses=ERRORS)
async def get_job(job_id: str) -> dict[str, Any]:
    return jobs.get_job(job_id)


@app.get("/api/v1/tickets", tags=["ops"])
async def list_tickets(status: str | None = "open") -> dict[str, Any]:
    sql, params = "SELECT * FROM tickets", []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    return {"tickets": get_db().all(sql + " ORDER BY created_at DESC LIMIT 200", params)}


@app.post("/api/v1/uploads", tags=["files"], status_code=201)
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a media file; use the returned URL as a media URL in content."""
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise ApiError("VALIDATION_ERROR", "files up to 200 MB")
    key = f"uploads/{uuid.uuid4().hex[:12]}/{Path(file.filename or 'file').name}"
    return {"key": key, "url": get_storage().put_bytes(key, data, file.content_type), "bytes": len(data),
            "content_type": file.content_type}


@app.get("/api/v1/files/{key:path}", tags=["files"], include_in_schema=False)
async def get_file(key: str, expires: int, sig: str) -> Response:
    storage = get_storage()
    if not isinstance(storage, LocalStorage) or not verify_link(key, expires, sig):
        raise ApiError("NOT_FOUND", "link is invalid or expired")
    try:
        data = storage.get_bytes(key)
    except FileNotFoundError:
        raise ApiError("NOT_FOUND", "file not found") from None
    import mimetypes

    return Response(data, media_type=mimetypes.guess_type(key)[0] or "application/octet-stream")


@app.get("/api/v1/feeds/{feed_key}.xml", tags=["files"])
async def get_feed(feed_key: str) -> Response:
    """The MRSS feed that CTV apps and aggregators poll."""
    try:
        feed = json.loads(get_storage().get_bytes(f"feeds/{feed_key}.json"))
    except Exception:  # noqa: BLE001
        raise ApiError("NOT_FOUND", f"feed {feed_key} not found") from None
    return Response(render_feed(feed), media_type="application/rss+xml")


# ---------------------------------------------------------------- simulation controls
def _mock_target(body: SimFaultIn) -> tuple[str, str]:
    if body.destination_id:
        row = store.get_destination(body.destination_id)
        cfg = row.get("config") or {}

        base = cfg.get("base_url") or cfg.get("api_base_url") or {"devto": "https://dev.to", "slack": "https://slack.com",
                                                                   "hashnode": "https://gql.hashnode.com"}.get(row["connector_key"], "")
        return row["connector_key"], urlsplit(base).netloc
    if body.platform and body.host:
        return body.platform, body.host
    raise ApiError("VALIDATION_ERROR", "give destination_id, or platform and host")


async def _mock_call(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    if s.mock_base_url:
        async with httpx.AsyncClient(timeout=10) as client:
            return (await client.post(s.mock_base_url.rstrip("/") + path, json=payload)).json()
    from app.mocks.server import app as mock_app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock") as client:
        return (await client.post(path, json=payload)).json()


@app.post("/api/v1/sim/faults", tags=["simulation"], responses=ERRORS)
async def sim_fault(body: SimFaultIn) -> dict[str, Any]:
    """Inject a fault into a mock platform to reproduce a hard case (timeout, 429, expired token, rejected content,
    renamed field, revoked permission). Works for mock-backed destinations."""
    platform, host = _mock_target(body)
    return await _mock_call("/_control/faults", {"platform": platform, "host": host, "kind": body.kind, "op": body.op,
                                                 "count": body.count, "retry_after": body.retry_after})


@app.post("/api/v1/sim/restore", tags=["simulation"], responses=ERRORS)
async def sim_restore(body: SimFaultIn) -> dict[str, Any]:
    platform, host = _mock_target(body)
    return await _mock_call("/_control/restore", {"platform": platform, "host": host})


@app.get("/api/v1/sim/state", tags=["simulation"], responses=ERRORS)
async def sim_state(destination_id: str) -> dict[str, Any]:
    """What the mock platform holds for this destination (to count posts, e.g. after a retry test)."""
    platform, host = _mock_target(SimFaultIn(destination_id=destination_id, kind="timeout"))
    s = get_settings()
    params = {"platform": platform, "host": host}
    if s.mock_base_url:
        async with httpx.AsyncClient(timeout=10) as client:
            return (await client.get(s.mock_base_url.rstrip("/") + "/_control/state", params=params)).json()
    from app.mocks.server import app as mock_app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock") as client:
        return (await client.get("/_control/state", params=params)).json()
