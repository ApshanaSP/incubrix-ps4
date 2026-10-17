"""Mock platforms for simulation mode (must-have 7) and the hard cases.

Each mock follows the shape of the real platform API closely enough that the
same connector code runs against the mock and the real thing. Every
(platform, host) pair is a separate instance with its own posts, so any number
of destinations can be simulated without code.

Fault injection (POST /_control/faults) reproduces the hard cases on demand:
  timeout_after_create  the post is created, then the answer never arrives
  timeout               the call hangs before doing anything
  rate_limit            429 with a reset time (retry_after seconds)
  auth_expired          401 / token_expired
  reject                the platform refuses the content (422 or equivalent)
  rename_field          the platform renamed a field in its response
  revoke                partner revoked permission (403)
  server_error          500
Faults apply to an operation (publish | status | delete | list | any) for
`count` calls (default 1).

Run standalone:  uvicorn app.mocks.server:app --port 9000
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="IncuBrix mock platforms", version="1.0.0")

HANG_SECONDS = 120.0


@dataclass
class Instance:
    platform: str
    host: str
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    faults: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    ids: Any = field(default_factory=lambda: itertools.count(1001))
    # simple platform-side rate limit: requests per window
    window_started: float = field(default_factory=time.time)
    window_count: int = 0
    revoked: bool = False


INSTANCES: dict[tuple[str, str], Instance] = {}
# mock rate limit (per instance); generous so normal tests never hit it
MOCK_LIMIT = {"requests": 300, "per_seconds": 60}


def instance(platform: str, host: str) -> Instance:
    key = (platform, host.lower())
    if key not in INSTANCES:
        INSTANCES[key] = Instance(platform, host.lower())
    return INSTANCES[key]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- control API
@app.post("/_control/faults")
async def add_fault(payload: dict[str, Any]) -> dict[str, Any]:
    inst = instance(payload["platform"], payload["host"])
    fault = {"kind": payload["kind"], "op": payload.get("op", "any"), "count": int(payload.get("count", 1)),
             "retry_after": float(payload.get("retry_after", 2)), "status": payload.get("status")}
    if fault["kind"] == "revoke":
        inst.revoked = True
    else:
        inst.faults.append(fault)
    return {"ok": True, "instance": f"{inst.platform}/{inst.host}", "pending_faults": inst.faults, "revoked": inst.revoked}


@app.post("/_control/reset")
async def reset(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if payload and payload.get("platform"):
        INSTANCES.pop((payload["platform"], payload["host"].lower()), None)
    else:
        INSTANCES.clear()
    return {"ok": True}


@app.post("/_control/restore")
async def restore(payload: dict[str, Any]) -> dict[str, Any]:
    inst = instance(payload["platform"], payload["host"])
    inst.revoked = False
    inst.faults.clear()
    return {"ok": True}


@app.get("/_control/state")
async def state(platform: str, host: str) -> dict[str, Any]:
    inst = instance(platform, host)
    live = [p for p in inst.posts.values() if not p.get("deleted")]
    return {"platform": platform, "host": host, "posts_total": len(inst.posts), "posts_live": len(live),
            "posts": list(inst.posts.values()), "faults": inst.faults, "revoked": inst.revoked,
            "calls": inst.calls[-50:]}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- dispatcher
class Fault(Exception):
    def __init__(self, response: Response | None = None, hang: bool = False, rename: bool = False) -> None:
        self.response = response
        self.hang = hang
        self.rename = rename


def take_fault(inst: Instance, op: str) -> dict[str, Any] | None:
    for f in inst.faults:
        if f["op"] in (op, "any") and f["count"] > 0:
            f["count"] -= 1
            if f["count"] <= 0:
                inst.faults.remove(f)
            return f
    return None


@app.api_route("/sim/{platform}/{host}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def dispatch(platform: str, host: str, path: str, request: Request) -> Response:
    inst = instance(platform, host)
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    query = {k: v[0] for k, v in parse_qs(request.url.query).items()}
    ctx = Ctx(inst, request.method, "/" + path, request.headers, body, query, raw)
    inst.calls.append({"method": request.method, "path": "/" + path, "at": now_iso()})
    # platform-side rate limit
    t = time.time()
    if t - inst.window_started > MOCK_LIMIT["per_seconds"]:
        inst.window_started, inst.window_count = t, 0
    inst.window_count += 1
    if inst.window_count > MOCK_LIMIT["requests"]:
        wait = int(MOCK_LIMIT["per_seconds"] - (t - inst.window_started)) + 1
        return JSONResponse({"errors": ["rate limited"]}, status_code=429, headers={"Retry-After": str(wait)})
    handler = HANDLERS.get(platform) or (template_rest if _uses_template_mock(platform) else None)
    if handler is None:
        return JSONResponse({"error": f"no mock for {platform}"}, status_code=404)
    try:
        return await handler(ctx)
    except Fault as f:
        if f.hang:
            await asyncio.sleep(HANG_SECONDS)
        return f.response or JSONResponse({"error": "fault"}, status_code=500)


@dataclass
class Ctx:
    inst: Instance
    method: str
    path: str
    headers: Any
    body: Any
    query: dict[str, str]
    raw: bytes

    def fault(self, op: str, styles: dict[str, Callable[[dict[str, Any]], Response]]) -> dict[str, Any] | None:
        """Apply a pending fault. Returns the fault if it must be applied after
        the work (timeout_after_create, rename_field), else raises."""
        f = take_fault(self.inst, op)
        if not f:
            return None
        kind = f["kind"]
        if kind == "timeout":
            raise Fault(hang=True)
        if kind in ("timeout_after_create", "rename_field"):
            return f
        if kind in styles:
            raise Fault(styles[kind](f))
        if kind == "rate_limit":
            raise Fault(JSONResponse({"errors": ["You've performed this action too many times."],
                                      "extras": {"wait_seconds": f["retry_after"]}}, status_code=429,
                                     headers={"Retry-After": str(int(f["retry_after"]))}))
        if kind == "auth_expired":
            raise Fault(JSONResponse({"errors": ["token expired"]}, status_code=401))
        if kind == "reject":
            raise Fault(JSONResponse({"errors": ["content rejected by moderation"]}, status_code=422))
        if kind == "server_error":
            raise Fault(JSONResponse({"errors": ["internal error"]}, status_code=500))
        return None


async def after(ctx: Ctx, fault: dict[str, Any] | None, payload: dict[str, Any], status: int = 200,
                rename: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> Response:
    if fault and fault["kind"] == "timeout_after_create":
        await asyncio.sleep(HANG_SECONDS)  # the work is done; the answer never arrives
    if fault and fault["kind"] == "rename_field" and rename:
        payload = rename(payload)
    return JSONResponse(payload, status_code=status)


def _token_ok(value: str | None) -> bool:
    return bool(value) and "expired" not in value and "revoked" not in value


# ---------------------------------------------------------------- Discourse
async def discourse(ctx: Ctx) -> Response:
    inst, m, p = ctx.inst, ctx.method, ctx.path
    if p == "/about.json":
        return JSONResponse({"about": {"title": f"Forum {inst.host}", "stats": {"users_count": 1840, "topics_count": 312}}})
    if not _token_ok(ctx.headers.get("api-key")) or not ctx.headers.get("api-username"):
        return JSONResponse({"errors": ["You are not permitted to view the requested resource. The API key is invalid."],
                             "error_type": "invalid_access"}, status_code=403)
    user = ctx.headers.get("api-username")
    if m == "POST" and p == "/posts.json":
        f = ctx.fault("publish", {})
        title, raw = ctx.body.get("title", ""), ctx.body.get("raw", "")
        errors = []
        if len(title) < 15:
            errors.append("Title is too short (minimum is 15 characters)")
        if len(title) > 255:
            errors.append("Title is too long (maximum is 255 characters)")
        if len(raw) < 20:
            errors.append("Body is too short (minimum is 20 characters)")
        if errors:
            return JSONResponse({"action": "create_post", "errors": errors}, status_code=422)
        pid = next(inst.ids)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "topic"
        post = {"id": pid, "topic_id": pid + 50000, "topic_slug": slug, "post_number": 1, "username": user,
                "raw": raw, "title": title, "created_at": now_iso(), "deleted_at": None, "hidden": False,
                "category": ctx.body.get("category"), "views": 0, "like_count": 0, "reply_count": 0}
        inst.posts[str(pid)] = post
        return await after(ctx, f, {k: post[k] for k in ("id", "topic_id", "topic_slug", "post_number", "username",
                                                          "created_at")},
                           rename=lambda d: {**{k: v for k, v in d.items() if k != "topic_id"}, "topicId": d["topic_id"]})
    mm = re.fullmatch(r"/posts/(\d+)\.json", p)
    if mm:
        post = inst.posts.get(mm.group(1))
        if not post:
            return JSONResponse({"errors": ["The requested URL or resource could not be found."]}, status_code=404)
        if m == "GET":
            f = ctx.fault("status", {})
            return await after(ctx, f, post, rename=lambda d: {**{k: v for k, v in d.items() if k != "topic_id"},
                                                                 "topicId": d["topic_id"]})
        if m == "DELETE":
            ctx.fault("delete", {})
            post["deleted_at"] = now_iso()
            post["deleted"] = True
            return JSONResponse({"success": "OK"})
    mt = re.fullmatch(r"/t/(\d+)\.json", p)
    if mt and m == "GET":
        post = next((x for x in inst.posts.values() if str(x["topic_id"]) == mt.group(1)), None)
        if not post or post.get("deleted"):
            return JSONResponse({"errors": ["not found"]}, status_code=404)
        post["views"] += 7
        return JSONResponse({"id": post["topic_id"], "title": post["title"], "views": post["views"],
                             "like_count": post["like_count"] + 2, "posts_count": 1 + post["reply_count"] + 1,
                             "reply_count": post["reply_count"] + 1})
    mu = re.fullmatch(r"/topics/created-by/([^/]+)\.json", p)
    if mu and m == "GET":
        ctx.fault("list", {})
        topics = [{"id": x["topic_id"], "title": x["title"], "slug": x["topic_slug"], "created_at": x["created_at"],
                   "first_post_id": x["id"]}
                  for x in sorted(inst.posts.values(), key=lambda x: -x["id"])
                  if x["username"] == mu.group(1) and not x.get("deleted")]
        return JSONResponse({"topic_list": {"topics": topics[:30]}})
    return JSONResponse({"errors": ["not found"]}, status_code=404)


# ---------------------------------------------------------------- Lemmy (API v3)
LEMMY_COMMUNITIES = {"incubrix_test": 3, "ai_creators": 4, "videomakers": 5}


async def lemmy(ctx: Ctx) -> Response:
    inst, m, p = ctx.inst, ctx.method, ctx.path
    if p == "/api/v3/user/login" and m == "POST":
        if ctx.body.get("password") in (None, "", "wrong"):
            return JSONResponse({"error": "incorrect_login"}, status_code=400)
        return JSONResponse({"jwt": "simjwt." + uuid.uuid4().hex, "registration_created": False, "verify_email_sent": False})
    if p == "/api/v3/community" and m == "GET":
        name = ctx.query.get("name", "")
        cid = LEMMY_COMMUNITIES.get(name, 100 + abs(hash(name)) % 900)
        return JSONResponse({"community_view": {"community": {"id": cid, "name": name, "actor_id":
                             f"https://{inst.host}/c/{name}", "posting_restricted_to_mods": False},
                             "counts": {"subscribers": 950 + cid, "posts": 40}}})
    auth = ctx.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not _token_ok(auth):
        return JSONResponse({"error": "not_logged_in"}, status_code=401)
    lemmy_styles = {
        "auth_expired": lambda f: JSONResponse({"error": "not_logged_in"}, status_code=401),
        "reject": lambda f: JSONResponse({"error": "couldnt_create_post"}, status_code=400),
        "rate_limit": lambda f: JSONResponse({"error": "rate_limit_error"}, status_code=429,
                                             headers={"Retry-After": str(int(f["retry_after"]))}),
    }

    def view(post: dict[str, Any]) -> dict[str, Any]:
        return {"post_view": {"post": {k: post[k] for k in ("id", "name", "body", "url", "ap_id", "deleted", "removed",
                                                             "published", "community_id")},
                              "counts": {"score": post["score"], "upvotes": post["score"], "downvotes": 0,
                                         "comments": post["comments"]},
                              "creator": {"name": post["creator"]}}}

    def renamed(d: dict[str, Any]) -> dict[str, Any]:
        return {"postView": d["post_view"]}

    if p == "/api/v3/post" and m == "POST":
        f = ctx.fault("publish", lemmy_styles)
        name = ctx.body.get("name", "")
        if not name or len(name) > 200:
            return JSONResponse({"error": "invalid_post_title"}, status_code=400)
        if len(ctx.body.get("body") or "") > 10000:
            return JSONResponse({"error": "invalid_body_field"}, status_code=400)
        pid = next(inst.ids)
        post = {"id": pid, "name": name, "body": ctx.body.get("body"), "url": ctx.body.get("url"),
                "ap_id": f"https://{inst.host}/post/{pid}", "deleted": False, "removed": False,
                "published": now_iso(), "community_id": ctx.body.get("community_id"), "score": 1, "comments": 0,
                "creator": "incubrix_bot"}
        inst.posts[str(pid)] = post
        return await after(ctx, f, view(post), rename=renamed)
    if p == "/api/v3/post" and m == "GET":
        f = ctx.fault("status", lemmy_styles)
        post = inst.posts.get(ctx.query.get("id", ""))
        if not post:
            return JSONResponse({"error": "couldnt_find_post"}, status_code=404)
        post["score"] += 1
        post["comments"] += 1
        return await after(ctx, f, view(post), rename=renamed)
    if p == "/api/v3/post/delete" and m == "POST":
        ctx.fault("delete", lemmy_styles)
        post = inst.posts.get(str(ctx.body.get("post_id")))
        if not post:
            return JSONResponse({"error": "couldnt_find_post"}, status_code=404)
        post["deleted"] = bool(ctx.body.get("deleted", True))
        return JSONResponse(view(post))
    if p == "/api/v3/user" and m == "GET":
        ctx.fault("list", lemmy_styles)
        posts = [view(x)["post_view"] for x in sorted(inst.posts.values(), key=lambda x: -x["id"]) if not x["deleted"]]
        return JSONResponse({"person_view": {"person": {"name": ctx.query.get("username")}}, "posts": posts[:20],
                             "comments": []})
    return JSONResponse({"error": "not_found"}, status_code=404)


# ---------------------------------------------------------------- Dev.to (Forem)
async def devto(ctx: Ctx) -> Response:
    inst, m, p = ctx.inst, ctx.method, ctx.path
    if not _token_ok(ctx.headers.get("api-key")):
        return JSONResponse({"error": "unauthorized", "status": 401}, status_code=401)
    styles = {"reject": lambda f: JSONResponse({"error": "Tag \"x\" is not allowed", "status": 422}, status_code=422)}
    if p == "/api/articles" and m == "POST":
        f = ctx.fault("publish", styles)
        art = ctx.body.get("article", {})
        title = art.get("title", "")
        tags = art.get("tags") or []
        if not title or len(title) > 128:
            return JSONResponse({"error": "Title is too long (maximum is 128 characters)", "status": 422}, status_code=422)
        if len(tags) > 4:
            return JSONResponse({"error": "Tag list exceed the maximum of 4 tags", "status": 422}, status_code=422)
        aid = next(inst.ids)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] + f"-{aid:x}"
        post = {"id": aid, "title": title, "slug": slug, "url": f"https://{inst.host}/incubrix/{slug}",
                "published": bool(art.get("published", False)), "body_markdown": art.get("body_markdown", ""),
                "tags": tags, "canonical_url": art.get("canonical_url"), "created_at": now_iso(),
                "page_views_count": 0, "public_reactions_count": 0, "comments_count": 0}
        inst.posts[str(aid)] = post
        out = {k: post[k] for k in ("id", "title", "slug", "url", "published", "canonical_url", "created_at")}
        return await after(ctx, f, out, status=201,
                           rename=lambda d: {**{k: v for k, v in d.items() if k != "url"}, "link": d["url"]})
    mm = re.fullmatch(r"/api/articles/(\d+)", p)
    if mm:
        post = inst.posts.get(mm.group(1))
        if not post:
            return JSONResponse({"error": "not found", "status": 404}, status_code=404)
        if m == "GET":
            f = ctx.fault("status", styles)
            if not post["published"]:
                return JSONResponse({"error": "not found", "status": 404}, status_code=404)
            out = {k: post[k] for k in ("id", "title", "url", "published", "canonical_url")}
            return await after(ctx, f, out, rename=lambda d: {**{k: v for k, v in d.items() if k != "url"},
                                                                "link": d["url"]})
        if m == "PUT":
            ctx.fault("delete", styles)
            art = ctx.body.get("article", {})
            if "published" in art:
                post["published"] = bool(art["published"])
                post["deleted"] = not post["published"]
            return JSONResponse({k: post[k] for k in ("id", "title", "url", "published")})
    if p in ("/api/articles/me/all", "/api/articles/me/published") and m == "GET":
        ctx.fault("list", styles)
        rows = []
        for x in sorted(inst.posts.values(), key=lambda x: -x["id"]):
            if p.endswith("published") and not x["published"]:
                continue
            x["page_views_count"] += 11
            rows.append({"id": x["id"], "title": x["title"], "url": x["url"], "published": x["published"],
                         "page_views_count": x["page_views_count"], "public_reactions_count": 3,
                         "comments_count": 1, "body_markdown": x["body_markdown"]})
        return JSONResponse(rows[: int(ctx.query.get("per_page", 30))])
    return JSONResponse({"error": "not found", "status": 404}, status_code=404)


# ---------------------------------------------------------------- Ghost Admin API
async def ghost(ctx: Ctx) -> Response:
    inst, m, p = ctx.inst, ctx.method, ctx.path
    if p == "/ghost/api/admin/site/":  # public in real Ghost too
        return JSONResponse({"site": {"title": f"Ghost {inst.host}", "url": f"https://{inst.host}/", "version": "5.96"}})
    auth = ctx.headers.get("authorization", "")
    if not auth.startswith("Ghost ") or not _token_ok(auth) or auth.count(".") != 2:
        return JSONResponse({"errors": [{"message": "Authorization failed", "type": "UnauthorizedError"}]}, status_code=401)
    styles = {"auth_expired": lambda f: JSONResponse({"errors": [{"message": "Token expired", "type": "UnauthorizedError"}]},
                                                     status_code=401),
              "reject": lambda f: JSONResponse({"errors": [{"message": "Validation error, cannot save post.",
                                                            "type": "ValidationError"}]}, status_code=422)}
    base = "/ghost/api/admin/posts/"
    if p == base and m == "POST":
        f = ctx.fault("publish", styles)
        src = (ctx.body.get("posts") or [{}])[0]
        title = src.get("title", "")
        if not title or len(title) > 255:
            return JSONResponse({"errors": [{"message": "Validation error, cannot save post.",
                                             "context": "Value in [posts.title] exceeds maximum length of 255 characters.",
                                             "type": "ValidationError"}]}, status_code=422)
        pid = uuid.uuid4().hex[:24]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
        post = {"id": pid, "uuid": str(uuid.uuid4()), "title": title, "slug": slug, "status": src.get("status", "draft"),
                "url": f"https://{inst.host}/{slug}/", "html": src.get("html"), "canonical_url": src.get("canonical_url"),
                "created_at": now_iso(), "tags": src.get("tags", [])}
        inst.posts[pid] = post
        return await after(ctx, f, {"posts": [post]}, status=201, rename=lambda d: {"items": d["posts"]})
    mm = re.fullmatch(r"/ghost/api/admin/posts/([0-9a-f]{24})/", p)
    if mm:
        post = inst.posts.get(mm.group(1))
        if not post or post.get("deleted"):
            return JSONResponse({"errors": [{"message": "Resource not found error, cannot read post.",
                                             "type": "NotFoundError"}]}, status_code=404)
        if m == "GET":
            f = ctx.fault("status", styles)
            return await after(ctx, f, {"posts": [post]}, rename=lambda d: {"items": d["posts"]})
        if m == "DELETE":
            ctx.fault("delete", styles)
            post["deleted"] = True
            return Response(status_code=204)
    if p == base and m == "GET":
        ctx.fault("list", styles)
        rows = [x for x in sorted(inst.posts.values(), key=lambda x: x["created_at"], reverse=True) if not x.get("deleted")]
        return JSONResponse({"posts": rows[:15], "meta": {"pagination": {"total": len(rows)}}})
    return JSONResponse({"errors": [{"message": "not found", "type": "NotFoundError"}]}, status_code=404)


# ---------------------------------------------------------------- partner webhook (HMAC)
# the partner's copy of each shared secret (simulation); register more with /_control/secrets
SIM_DEFAULT_SECRET = "whsec_simulation_only"
PARTNER_SECRETS: dict[str, str] = {}


@app.post("/_control/secrets")
async def set_secret(payload: dict[str, Any]) -> dict[str, Any]:
    PARTNER_SECRETS[payload["host"].lower()] = payload["secret"]
    return {"ok": True}


def _verify_sig(ctx: Ctx) -> Response | None:
    ts = ctx.headers.get("x-incubrix-timestamp", "")
    sig = ctx.headers.get("x-incubrix-signature", "")
    secret = PARTNER_SECRETS.get(ctx.inst.host, SIM_DEFAULT_SECRET)
    if ctx.inst.revoked:
        return JSONResponse({"error": "permission_revoked", "message": "Partner revoked IncuBrix's access"},
                            status_code=403)
    if not ts or not sig.startswith("sha256="):
        return JSONResponse({"error": "missing_signature"}, status_code=401)
    if abs(time.time() - int(ts)) > 300:
        return JSONResponse({"error": "stale_timestamp"}, status_code=401)
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + ctx.raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig[7:]):
        return JSONResponse({"error": "bad_signature"}, status_code=401)
    return None


async def webhook(ctx: Ctx) -> Response:
    inst, m, p = ctx.inst, ctx.method, ctx.path
    bad = _verify_sig(ctx)
    if bad:
        return bad
    if p == "/incubrix/posts" and m == "POST":
        f = ctx.fault("publish", {})
        key = ctx.headers.get("idempotency-key", "")
        existing = next((x for x in inst.posts.values() if key and x["idempotency_key"] == key), None)
        if existing:  # the partner protocol requires de-duplication on Idempotency-Key
            return JSONResponse({"id": existing["id"], "url": existing["url"], "status": existing["status"]})
        if len(ctx.body.get("title", "")) > 200:
            return JSONResponse({"error": "title_too_long"}, status_code=422)
        pid = f"wp{next(inst.ids)}"
        post = {"id": pid, "url": f"https://{inst.host}/news/{pid}", "status": "live", "idempotency_key": key,
                "title": ctx.body.get("title"), "created_at": now_iso()}
        inst.posts[pid] = post
        return await after(ctx, f, {"id": pid, "url": post["url"], "status": "live"}, status=201,
                           rename=lambda d: {"id": d["id"], "href": d["url"], "status": d["status"]})
    if p == "/incubrix/posts" and m == "GET":
        key = ctx.query.get("idempotency_key")
        rows = [x for x in inst.posts.values() if x["idempotency_key"] == key and not x.get("deleted")]
        return JSONResponse({"posts": [{"id": x["id"], "url": x["url"], "status": x["status"]} for x in rows]})
    mm = re.fullmatch(r"/incubrix/posts/([a-z0-9]+)", p)
    if mm:
        post = inst.posts.get(mm.group(1))
        if not post or post.get("deleted"):
            return JSONResponse({"error": "not_found"}, status_code=404)
        if m == "GET":
            f = ctx.fault("status", {})
            return await after(ctx, f, {"id": post["id"], "url": post["url"], "status": post["status"],
                                        "views": 40, "clicks": 6},
                               rename=lambda d: {**{k: v for k, v in d.items() if k != "url"}, "href": d["url"]})
        if m == "DELETE":
            ctx.fault("delete", {})
            post["deleted"] = True
            return JSONResponse({"deleted": True})
    return JSONResponse({"error": "not_found"}, status_code=404)


# ---------------------------------------------------------------- Hashnode (GraphQL)
async def hashnode(ctx: Ctx) -> Response:
    inst = ctx.inst
    if not _token_ok(ctx.headers.get("authorization")):
        return JSONResponse({"errors": [{"message": "You must be authenticated.", "extensions": {"code": "UNAUTHENTICATED"}}],
                             "data": None})
    q = ctx.body.get("query", "")
    v = ctx.body.get("variables", {}) or {}
    styles = {"auth_expired": lambda f: JSONResponse({"errors": [{"message": "Token expired",
                                                                  "extensions": {"code": "UNAUTHENTICATED"}}]}),
              "reject": lambda f: JSONResponse({"errors": [{"message": "Content violates guidelines",
                                                            "extensions": {"code": "BAD_USER_INPUT"}}]})}
    if "publishPost" in q:
        f = ctx.fault("publish", styles)
        inp = v.get("input", {})
        if len(inp.get("title", "")) > 250:
            return JSONResponse({"errors": [{"message": "title too long", "extensions": {"code": "BAD_USER_INPUT"}}]})
        pid = uuid.uuid4().hex[:24]
        slug = re.sub(r"[^a-z0-9]+", "-", inp.get("title", "").lower()).strip("-")[:50]
        post = {"id": pid, "slug": slug, "url": f"https://incubrix.hashnode.dev/{slug}", "title": inp.get("title"),
                "views": 0, "reactionCount": 0, "responseCount": 0, "publishedAt": now_iso()}
        inst.posts[pid] = post
        return await after(ctx, f, {"data": {"publishPost": {"post": {k: post[k] for k in ("id", "slug", "url", "title")}}}},
                           rename=lambda d: {"data": {"publishPost": {"article": d["data"]["publishPost"]["post"]}}})
    if "removePost" in q:
        ctx.fault("delete", styles)
        post = inst.posts.get(v.get("input", {}).get("id", ""))
        if not post:
            return JSONResponse({"errors": [{"message": "Post not found", "extensions": {"code": "NOT_FOUND"}}]})
        post["deleted"] = True
        return JSONResponse({"data": {"removePost": {"post": {"id": post["id"]}}}})
    if "me" in q and "posts" in q:
        rows = [{"id": x["id"], "title": x["title"], "url": x["url"]} for x in inst.posts.values() if not x.get("deleted")]
        return JSONResponse({"data": {"me": {"posts": {"nodes": rows[-20:]}}}})
    if "post(" in q:
        f = ctx.fault("status", styles)
        post = inst.posts.get(v.get("id", ""))
        if not post or post.get("deleted"):
            return JSONResponse({"data": {"post": None}})
        post["views"] += 13
        out = {"id": post["id"], "url": post["url"], "views": post["views"], "reactionCount": 4, "responseCount": 1}
        return await after(ctx, f, {"data": {"post": out}},
                           rename=lambda d: {"data": {"post": {**{k: x for k, x in d["data"]["post"].items() if k != "url"},
                                                               "permalink": d["data"]["post"]["url"]}}})
    return JSONResponse({"errors": [{"message": "unknown operation"}]})


# ---------------------------------------------------------------- Slack Web API
async def slack(ctx: Ctx) -> Response:
    inst, p = ctx.inst, ctx.path
    auth = ctx.headers.get("authorization", "")
    if not auth.startswith("Bearer xoxb-"):
        return JSONResponse({"ok": False, "error": "invalid_auth"})
    if "expired" in auth:
        return JSONResponse({"ok": False, "error": "token_expired"})
    if "revoked" in auth:
        return JSONResponse({"ok": False, "error": "token_revoked"})
    styles = {"auth_expired": lambda f: JSONResponse({"ok": False, "error": "token_expired"}),
              "reject": lambda f: JSONResponse({"ok": False, "error": "msg_blocks_too_long"}),
              "rate_limit": lambda f: JSONResponse({"ok": False, "error": "ratelimited"}, status_code=429,
                                                   headers={"Retry-After": str(int(f["retry_after"]))})}
    args = {**ctx.query, **(ctx.body if isinstance(ctx.body, dict) else {})}
    if p == "/api/chat.postMessage":
        f = ctx.fault("publish", styles)
        if len(args.get("text", "")) > 40000:
            return JSONResponse({"ok": False, "error": "msg_too_long"})
        if not args.get("channel"):
            return JSONResponse({"ok": False, "error": "channel_not_found"})
        ts = f"{int(time.time())}.{next(inst.ids):06d}"
        msg = {"ts": ts, "channel": args["channel"], "text": args.get("text", ""), "metadata": args.get("metadata"),
               "reactions": [{"name": "fire", "count": 3}], "reply_count": 2}
        inst.posts[ts] = msg
        return await after(ctx, f, {"ok": True, "channel": args["channel"], "ts": ts, "message": {"text": msg["text"]}},
                           rename=lambda d: {**{k: v for k, v in d.items() if k != "ts"}, "timestamp": d["ts"]})
    if p == "/api/chat.getPermalink":
        f = ctx.fault("status", styles)
        msg = inst.posts.get(args.get("message_ts", ""))
        if not msg or msg.get("deleted"):
            return JSONResponse({"ok": False, "error": "message_not_found"})
        return await after(ctx, f, {"ok": True, "channel": msg["channel"],
                                    "permalink": f"https://incubrix-sim.slack.com/archives/{msg['channel']}/p{msg['ts'].replace('.', '')}"},
                           rename=lambda d: {**{k: v for k, v in d.items() if k != "permalink"}, "link": d["permalink"]})
    if p == "/api/chat.delete":
        ctx.fault("delete", styles)
        msg = inst.posts.get(args.get("ts", ""))
        if not msg or msg.get("deleted"):
            return JSONResponse({"ok": False, "error": "message_not_found"})
        msg["deleted"] = True
        return JSONResponse({"ok": True, "channel": msg["channel"], "ts": msg["ts"]})
    if p == "/api/conversations.history":
        rows = [x for x in inst.posts.values() if x["channel"] == args.get("channel") and not x.get("deleted")]
        return JSONResponse({"ok": True, "messages": sorted(rows, key=lambda x: x["ts"], reverse=True)[:50]})
    if p == "/api/reactions.get":
        msg = inst.posts.get(args.get("timestamp", ""))
        if not msg or msg.get("deleted"):
            return JSONResponse({"ok": False, "error": "message_not_found"})
        return JSONResponse({"ok": True, "type": "message", "message": {"ts": msg["ts"], "reactions": msg["reactions"],
                                                                        "reply_count": msg["reply_count"]}})
    if p == "/api/auth.test":
        return JSONResponse({"ok": True, "team": "IncuBrix Sim", "user": "incubrix_bot", "bot_id": "B0SIM"})
    return JSONResponse({"ok": False, "error": "unknown_method"})


# ---------------------------------------------------------------- generic community pages (rules, reachability)
async def site(ctx: Ctx) -> Response:
    """Plain web pages used by catalogue import and rules capture in simulation."""
    p = ctx.path
    if "rules" in p or "guidelines" in p or p.endswith("/faq"):
        html = (
            "<html><body><h1>Community rules</h1><ol>"
            "<li>No self-promotion. Do not advertise your product, service or company.</li>"
            "<li>Members may post at most one link post per week.</li>"
            "<li>No URL shorteners.</li>"
            "<li>Posts made with AI must be labelled 'AI-generated'.</li>"
            "<li>No bots or automated posting without moderator approval.</li>"
            "</ol></body></html>"
        )
        return Response(html, media_type="text/html")
    return Response("<html><head><title>Community</title></head><body>ok</body></html>", media_type="text/html")


# ---------------------------------------------------------------- template REST platform
async def template_rest(ctx: Ctx) -> Response:
    """The REST API that connectors made with `python -m tools.new_connector` speak, so a new connector certifies
    and runs every hard case before its real API is wired in:
      GET    /api/me                          who am I (certification probe)
      POST   /api/posts                       {title, body, link, tags} + Idempotency-Key -> 201 {id, url, status}
      GET    /api/posts?idempotency_key=K     {posts: [...]}   (find a post after a timeout)
      GET    /api/posts/{id}                  {id, url, status, stats: {views, likes, comments, shares, clicks}}
      DELETE /api/posts/{id}                  {deleted: true}
    Auth: Authorization: Bearer <api_key>; a token containing 'expired' or 'revoked' answers 401."""
    inst, m, p = ctx.inst, ctx.method, ctx.path
    token = (ctx.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not _token_ok(token) or inst.revoked:
        return JSONResponse({"error": "unauthorized"}, status_code=401 if not inst.revoked else 403)
    if p == "/api/me" and m == "GET":
        return JSONResponse({"ok": True, "name": "incubrix_bot"})
    if p == "/api/posts" and m == "POST":
        f = ctx.fault("publish", {})
        key = ctx.headers.get("idempotency-key", "")
        existing = next((x for x in inst.posts.values() if key and x["idempotency_key"] == key and not x.get("deleted")), None)
        if existing:
            return JSONResponse({"id": existing["id"], "url": existing["url"], "status": "live"})
        if len(ctx.body.get("title") or "") > 255:
            return JSONResponse({"error": "title is too long"}, status_code=422)
        pid = str(next(inst.ids))
        post = {"id": pid, "url": f"https://{inst.host}/posts/{pid}", "status": "live", "idempotency_key": key,
                "title": ctx.body.get("title"), "created_at": now_iso()}
        inst.posts[pid] = post
        return await after(ctx, f, {"id": pid, "url": post["url"], "status": "live"}, status=201,
                           rename=lambda d: {"id": d["id"], "permalink": d["url"], "status": d["status"]})
    if p == "/api/posts" and m == "GET":
        key = ctx.query.get("idempotency_key")
        rows = [x for x in inst.posts.values() if x["idempotency_key"] == key and not x.get("deleted")]
        return JSONResponse({"posts": [{"id": x["id"], "url": x["url"], "status": x["status"]} for x in rows]})
    mm = re.fullmatch(r"/api/posts/(\d+)", p)
    if mm:
        post = inst.posts.get(mm.group(1))
        if not post or post.get("deleted"):
            return JSONResponse({"error": "not found"}, status_code=404)
        if m == "GET":
            f = ctx.fault("status", {})
            return await after(ctx, f, {"id": post["id"], "url": post["url"], "status": post["status"],
                                        "stats": {"views": 12, "likes": 3, "comments": 1, "shares": 0, "clicks": 2}},
                               rename=lambda d: {**{k: v for k, v in d.items() if k != "url"}, "permalink": d["url"]})
        if m == "DELETE":
            ctx.fault("delete", {})
            post["deleted"] = True
            return JSONResponse({"deleted": True})
    return JSONResponse({"error": "not found"}, status_code=404)


def _uses_template_mock(platform: str) -> bool:
    """Connectors whose manifest says `mock: template_rest` are served by the template REST platform."""
    from app.connectors.base import MANIFEST_DIR

    f = MANIFEST_DIR / f"{platform}.yaml"
    return f.exists() and "mock: template_rest" in f.read_text(encoding="utf-8")


HANDLERS: dict[str, Callable[[Ctx], Any]] = {
    "discourse": discourse,
    "lemmy": lemmy,
    "devto": devto,
    "ghost": ghost,
    "webhook": webhook,
    "hashnode": hashnode,
    "slack": slack,
    "site": site,
}
