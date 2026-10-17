"""ASGI middleware: correlation id + access log, API token, rate limit, and
Idempotency-Key replay for every POST (Integration Standard points 4 and 8)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.db import get_db, now_iso
from app.errors import ApiError
from app.logs import correlation_id, log

logger = logging.getLogger("http")
OPEN_PATHS = ("/health", "/ready", "/docs", "/openapi.json", "/openapi.yaml", "/redoc", "/api/v1/files/",
              "/api/v1/feeds/", "/favicon.ico", "/ui/", "/")


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
            is_open = path in ("/", "/health", "/ready") or any(path.startswith(p) for p in OPEN_PATHS if p != "/")
            if s.api_token and not is_open:
                auth = headers.get("authorization", "")
                given = auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")
                if given != s.api_token:
                    err = ApiError("UNAUTHORIZED", "send 'Authorization: Bearer <token>' or 'X-API-Key: <token>'")
                    await _send(send_wrapper, *_json_response(401, err.to_dict()))
                    return
            client = headers.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("?",))[0]
            if s.rate_limit_per_minute and not path.startswith(("/health", "/ready")):
                now = time.time()
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
