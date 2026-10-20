"""ASGI middleware: correlation id + access log, API token, rate limit, and
Idempotency-Key replay for every POST (Integration Standard points 4 and 8)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.db import get_db, now_iso
from app.errors import ApiError
from app.logs import correlation_id, log

logger = logging.getLogger("http")
# Open even when API_TOKEN is set: health checks, the sign-in page, signed download links (their own signature
# protects them) and the public video feeds. Everything else (the dashboard, /docs, /openapi.yaml, the API) needs
# the token: a Bearer / X-API-Key header, or the session cookie the sign-in page sets.
OPEN_EXACT = ("/health", "/ready", "/favicon.ico", "/login", "/logout")
OPEN_PREFIXES = ("/api/v1/files/", "/api/v1/feeds/")
SESSION_COOKIE = "ibx_session"
SESSION_MAX_AGE_S = 30 * 24 * 3600
_failed_auth: list[float] = []  # wrong-token times, for the whole service


def session_value(api_token: str) -> str:
    """The session cookie holds a value derived from the token, never the token itself; changing API_TOKEN signs
    everyone out."""
    return hmac.new(api_token.encode(), b"ibx-dashboard-session", hashlib.sha256).hexdigest()


def _cookie(header: str, name: str) -> str:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def credentials_ok(headers: dict[str, str], api_token: str) -> tuple[bool, bool]:
    """(accepted, something was presented): a Bearer / X-API-Key token or the session cookie."""
    auth = headers.get("authorization", "")
    given = auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")
    if given and hmac.compare_digest(given.encode(), api_token.encode()):
        return True, True
    cookie = _cookie(headers.get("cookie", ""), SESSION_COOKIE)
    if cookie and hmac.compare_digest(cookie.encode(), session_value(api_token).encode()):
        return True, True
    return False, bool(given or cookie)


def wrong_token_lockout(now: float, limit: int) -> int:
    """Record one wrong token. Returns seconds to wait when the service-wide limit is reached, else 0. Wrong tokens
    are counted for the whole service, not per client: a client can choose its own X-Forwarded-For, so per-client
    counting alone would let it guess tokens at full speed."""
    _failed_auth[:] = [t for t in _failed_auth if now - t < 60]
    if limit and len(_failed_auth) >= limit:
        return int(60 - (now - _failed_auth[0])) + 1
    _failed_auth.append(now)
    return 0


def _json_response(status: int, payload: dict[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None):
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    return status, headers + (extra_headers or []), body


async def _send(send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestContext:
    """Sets X-Correlation-Id (taken from the request or generated), checks the
    API token, applies a per-client rate limit and logs each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.windows: dict[str, list[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        cid = headers.get("x-correlation-id") or uuid.uuid4().hex
        token = correlation_id.set(cid)
        started = time.monotonic()
        status_holder = {"status": 500}
        path = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"x-correlation-id", cid.encode())]
            await send(message)

        try:
            s = get_settings()
            now = time.time()
            is_open = path in OPEN_EXACT or path.startswith(OPEN_PREFIXES)
            if s.api_token and not is_open:
                accepted, presented = credentials_ok(headers, s.api_token)
                if not accepted:
                    retry = wrong_token_lockout(now, s.failed_auth_per_minute) if presented else 0
                    if retry:
                        err = ApiError("SERVICE_RATE_LIMIT", "too many requests with a wrong token; try again later",
                                       details={"retry_after_s": retry})
                        await _send(send_wrapper, *_json_response(429, err.to_dict(),
                                                                  [(b"retry-after", str(retry).encode())]))
                        return
                    if scope.get("method") == "GET" and "text/html" in headers.get("accept", ""):
                        # a person in a browser: send them to the sign-in page, then back here
                        target = path + ("?" + scope["query_string"].decode() if scope.get("query_string") else "")
                        await _send(send_wrapper, 303, [(b"location", ("/login?next=" + quote(target, safe="")).encode()),
                                                        (b"content-length", b"0")], b"")
                        return
                    err = ApiError("UNAUTHORIZED", "send 'Authorization: Bearer <token>' or 'X-API-Key: <token>', "
                                                   "or sign in at /login")
                    await _send(send_wrapper, *_json_response(401, err.to_dict()))
                    return
            client = headers.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("?",))[0]
            if s.rate_limit_per_minute and not path.startswith(("/health", "/ready")):
                if len(self.windows) > 10_000:  # forget idle clients so the table cannot grow without bound
                    self.windows = {c: w for c, w in self.windows.items() if w and now - w[-1] < 60}
                window = [t for t in self.windows.get(client, []) if now - t < 60]
                if len(window) >= s.rate_limit_per_minute:
                    retry = int(60 - (now - window[0])) + 1
                    err = ApiError("SERVICE_RATE_LIMIT", f"more than {s.rate_limit_per_minute} requests a minute",
                                   details={"retry_after_s": retry})
                    await _send(send_wrapper, *_json_response(429, err.to_dict(), [(b"retry-after", str(retry).encode())]))
                    return
                window.append(now)
                self.windows[client] = window
            await self.app(scope, receive, send_wrapper)
        finally:
            if not path.startswith(("/health", "/ready")):
                log(logger, logging.INFO, "request", method=scope.get("method"), path=path,
                    status=status_holder["status"], ms=int((time.monotonic() - started) * 1000))
            correlation_id.reset(token)


class Idempotency:
    """Same Idempotency-Key + same body on the same route -> the stored
    response is returned and the work is not done again. Same key with a
    different body -> 409 IDEMPOTENCY_KEY_CONFLICT. Concurrent duplicates wait
    for the first to finish."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.locks: dict[str, asyncio.Lock] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        key = headers.get("idempotency-key")
        if not key:
            await self.app(scope, receive, send)
            return
        # read the whole body so it can be hashed and replayed downstream
        chunks = []
        more = True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        route = scope.get("path", "")
        req_hash = hashlib.sha256(body + b"|" + headers.get("content-type", "").encode()).hexdigest()
        lock = self.locks.setdefault(f"{route}|{key}", asyncio.Lock())
        async with lock:
            db = get_db()
            row = db.one("SELECT * FROM idempotency WHERE key=? AND scope=?", [key, route])
            if row:
                if row["request_hash"] != req_hash:
                    err = ApiError("IDEMPOTENCY_KEY_CONFLICT", "this Idempotency-Key was already used with a different body",
                                   details={"route": route})
                    await _send(send, *_json_response(409, err.to_dict()))
                    return
                stored = row["response"] if isinstance(row["response"], (bytes, str)) else json.dumps(row["response"])
                data = stored.encode() if isinstance(stored, str) else stored
                await _send(send, row["status_code"], [(b"content-type", b"application/json"),
                                                       (b"content-length", str(len(data)).encode()),
                                                       (b"idempotent-replayed", b"true")], data)
                return

            sent = False

            async def replay_receive() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            captured: dict[str, Any] = {"status": 500, "body": b""}

            async def capture_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                elif message["type"] == "http.response.body":
                    captured["body"] += message.get("body", b"")
                await send(message)

            await self.app(scope, replay_receive, capture_send)
            if captured["status"] < 500:
                db.execute("INSERT OR REPLACE INTO idempotency(key, scope, request_hash, status_code, response, created_at) "
                           "VALUES (?,?,?,?,?,?)", [key, route, req_hash, captured["status"],
                                                    captured["body"].decode("utf-8", "replace"), now_iso()])
