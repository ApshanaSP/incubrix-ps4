"""Outbound HTTP for connectors: simulation routing, rate limits, 429 back-off,
error mapping and a per-destination circuit breaker.

Simulation: when SIMULATION_MODE is on, or the target host ends in ".mock",
the request is rewritten to the mock platforms as
    /sim/{connector_key}/{host}/{path}
and sent either in-process (no network at all) or to MOCK_BASE_URL. Each host
is its own mock instance, so adding forum number 41 needs no code.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.db import get_db
from app.errors import ConnectorError
from app.logs import log

logger = logging.getLogger("connector.http")


def is_simulated(url: str) -> bool:
    """For platform calls (posting, status, delete): in simulation mode every call goes to the mock platforms."""
    host = (urlsplit(url).hostname or "").lower()
    return get_settings().simulation_mode or host.endswith(".mock")


def is_mock_host(url: str) -> bool:
    """For reading public pages (rules pages, reachability, member counts): reading is never simulated, because
    it posts nothing. Only the built-in mock hosts (*.mock) are answered by the mock site."""
    return (urlsplit(url).hostname or "").lower().endswith(".mock")


def _sim_url(connector_key: str, url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    base = get_settings().mock_base_url.rstrip("/") or "http://mock.internal"
    return f"{base}/sim/{connector_key}/{parts.netloc}{path}{query}"


def _transport_for(url: str) -> httpx.AsyncBaseTransport | None:
    if is_simulated(url) and not get_settings().mock_base_url:
        from app.mocks.server import app as mock_app  # local import: mocks are optional at runtime

        return httpx.ASGITransport(app=mock_app)
    return None


# ---------------------------------------------------------------- rate limits
@dataclass
class _Bucket:
    capacity: int
    per_seconds: float
    tokens: float = 0.0
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)

    def wait_time(self) -> float:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.capacity / self.per_seconds)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) * self.per_seconds / self.capacity


_buckets: dict[str, _Bucket] = {}
_bucket_lock = asyncio.Lock()


async def _respect_rate_limit(scope: str, limits: dict[str, Any] | None) -> None:
    if not limits:
        return
    async with _bucket_lock:
        bucket = _buckets.get(scope)
        if bucket is None:
            bucket = _Bucket(int(limits.get("requests", 60)), float(limits.get("per_seconds", 60)))
            _buckets[scope] = bucket
        wait = bucket.wait_time()
    if wait > 0:
        await asyncio.sleep(wait)


def reset_after_seconds(resp: httpx.Response) -> float | None:
    """Read the platform's reset time from the usual headers."""
    h = resp.headers
    for name in ("retry-after", "x-ratelimit-reset-after", "ratelimit-reset", "x-rate-limit-reset", "x-ratelimit-reset"):
        raw = h.get(name)
        if not raw:
            continue
        raw = raw.strip()
        try:
            val = float(raw)
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(raw) if raw else None
            if parsed is None:
                continue
            return max(0.0, parsed.timestamp() - time.time())
        # epoch seconds vs. delta seconds
        return max(0.0, val - time.time()) if val > 1_000_000_000 else max(0.0, val)
    try:  # Discourse / Lemmy style JSON bodies
        body = resp.json()
        extras = body.get("extras", {}) if isinstance(body, dict) else {}
        if "wait_seconds" in extras:
            return float(extras["wait_seconds"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- breaker
def _breaker_get(connector_key: str, dest_scope: str) -> tuple[int, float | None]:
    """(consecutive failures, epoch time of the last failure)."""
    row = get_db().one(
        "SELECT consecutive_failures, last_failure_at FROM breaker WHERE connector_key=? AND destination_id=?",
        [connector_key, dest_scope],
    )
    return (int(row["consecutive_failures"]), row["last_failure_at"]) if row else (0, None)


def _breaker_set(connector_key: str, dest_scope: str, value: int, failed_at: float | None = None) -> None:
    get_db().execute(
        "INSERT INTO breaker(connector_key, destination_id, consecutive_failures, last_failure_at) VALUES(?,?,?,?) "
        "ON CONFLICT(connector_key, destination_id) DO UPDATE SET consecutive_failures=excluded.consecutive_failures, "
        "last_failure_at=excluded.last_failure_at",
        [connector_key, dest_scope, value, failed_at],
    )


def reset_breaker(connector_key: str, dest_scope: str) -> None:
    _breaker_set(connector_key, dest_scope, 0)


# a 429 whose answer says the daily/monthly allowance is used up (not a short burst limit)
QUOTA_WORDS = ("quota", "daily limit", "per day", "monthly limit")


# ---------------------------------------------------------------- client
class PlatformHTTP:
    """One instance per connector call. `scope` identifies the platform instance
    (its base URL) for rate limiting and the breaker."""

    def __init__(
        self,
        connector_key: str,
        scope: str,
        error_map: dict[str, str] | None = None,
        rate_limits: dict[str, Any] | None = None,
        max_429_waits: int = 2,
    ) -> None:
        self.connector_key = connector_key
        self.scope = scope
        self.error_map = {str(k): v for k, v in (error_map or {}).items()}
        self.rate_limits = rate_limits
        self.max_429_waits = max_429_waits

    async def request(
        self,
        method: str,
        url: str,
        *,
        expect: tuple[int, ...] = (200, 201, 202, 204),
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        s = get_settings()
        failures, last_failure = _breaker_get(self.connector_key, self.scope)
        if failures >= s.unavailable_trip_after:
            reopens_in = (last_failure or 0) + s.breaker_cooldown_s - time.time()
            if reopens_in > 0:
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE",
                    f"stopped calling {self.scope} after {failures} consecutive failures; one trial call is allowed "
                    f"in {int(reopens_in) + 1} s",
                    details={"breaker_open": True, "consecutive_failures": failures,
                             "retry_after_s": int(reopens_in) + 1},
                    retryable=False,
                )
            # half-open: this call is the trial. Success closes the breaker; a failure re-opens it for a cooldown.
            log(logger, logging.INFO, "breaker half-open; trial call", connector=self.connector_key, scope=self.scope)
        target = _sim_url(self.connector_key, url) if is_simulated(url) else url
        headers = {"User-Agent": s.user_agent, **kwargs.pop("headers", {})}
        if is_simulated(url):
            headers["X-Sim-Host"] = urlsplit(url).netloc
        waits = 0
        while True:
            await _respect_rate_limit(f"{self.connector_key}:{self.scope}", self.rate_limits)
            started = time.monotonic()
            limit = timeout or s.http_timeout_s
            try:
                async with httpx.AsyncClient(
                    transport=_transport_for(url), timeout=limit, follow_redirects=False
                ) as client:
                    # wait_for also bounds in-process (ASGI) calls, which ignore httpx timeouts
                    resp = await asyncio.wait_for(client.request(method, target, headers=headers, **kwargs), limit)
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                self._fail()
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE",
                    f"{method} {urlsplit(url).path} timed out",
                    details={"timeout": True, "outcome_unknown": method != "GET"},
                ) from exc
            except httpx.TransportError as exc:
                self._fail()
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE", f"cannot reach platform: {type(exc).__name__}", details={"network": True}
                ) from exc
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log(
                logger, logging.INFO, "platform call",
                connector=self.connector_key, method=method, path=urlsplit(url).path,
                status=resp.status_code, ms=elapsed_ms, simulated=is_simulated(url),
            )
            if resp.status_code == 429:
                wait = reset_after_seconds(resp)
                quota = any(w in _platform_message(resp).lower() for w in QUOTA_WORDS)
                if not quota and wait is not None and wait <= s.max_backoff_s and waits < self.max_429_waits:
                    waits += 1
                    log(logger, logging.WARNING, "rate limited; backing off", wait_s=round(wait, 2), attempt=waits)
                    await asyncio.sleep(wait)
                    continue
                raise self._mapped(resp, retry_after=wait)
            if 300 <= resp.status_code < 400:
                # an API call that is redirected means the API moved or its access rules changed (for example
                # Hashnode's GraphQL API, which since May 2026 redirects to its paid-plan announcement)
                loc = resp.headers.get("location", "")
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE",
                    f"the platform redirected this API call to {loc or 'another address'}: its API moved or its access "
                    "rules changed, so the call was not repeated",
                    retryable=False, details={"http_status": resp.status_code, "location": loc, "cause": "API_MOVED"},
                )
            if resp.status_code in expect:
                if resp.content and "html" in resp.headers.get("content-type", "").lower():
                    # a login wall, bot protection or a moved API answered with a web page, not the API
                    raise ConnectorError(
                        "PLATFORM_UNAVAILABLE",
                        "the platform answered with a web page instead of its API (a login wall, bot protection or a "
                        "moved API)", retryable=False,
                        details={"http_status": resp.status_code, "cause": "NOT_AN_API_RESPONSE",
                                 "outcome_unknown": method != "GET"},
                    )
                reset_breaker(self.connector_key, self.scope)
                return resp
            if resp.status_code >= 500:
                self._fail()
            raise self._mapped(resp)

    def _fail(self) -> None:
        failures, _ = _breaker_get(self.connector_key, self.scope)
        _breaker_set(self.connector_key, self.scope, failures + 1, time.time())

    def _mapped(self, resp: httpx.Response, retry_after: float | None = None) -> ConnectorError:
        status = str(resp.status_code)
        code = self.error_map.get(status)
        if code is None:
            if resp.status_code == 429:
                code = "PLATFORM_RATE_LIMIT"
            elif resp.status_code in (401, 403):
                code = "CONNECTION_AUTH_EXPIRED"
            elif resp.status_code in (400, 413, 415, 422):
                code = "PLATFORM_CONTENT_REJECTED"
            else:
                code = "PLATFORM_UNAVAILABLE"
        platform_message = _platform_message(resp)
        if code == "PLATFORM_RATE_LIMIT" and any(w in platform_message.lower() for w in QUOTA_WORDS):
            # the daily allowance is used up: waiting a few seconds will not help, reschedule to the next window
            code = "PLATFORM_QUOTA_EXHAUSTED"
        details: dict[str, Any] = {"http_status": resp.status_code, "platform_message": platform_message}
        if retry_after is not None:
            details["retry_after_s"] = round(retry_after, 2)
            details["reset_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + retry_after))
        return ConnectorError(code, f"platform answered {resp.status_code}: {platform_message}"[:500], details=details)


def _platform_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:300]
    if isinstance(body, dict):
        for key in ("errors", "error", "message", "detail"):
            if key in body:
                val = body[key]
                if isinstance(val, list):
                    return "; ".join(str(v.get("message", v)) if isinstance(v, dict) else str(v) for v in val)[:300]
                if isinstance(val, dict):
                    return str(val.get("message", val))[:300]
                return str(val)[:300]
    return str(body)[:300]


def require(obj: Any, *path: str | int) -> Any:
    """Read a field the connector depends on. If the platform renamed or removed
    it, fail loudly as schema drift instead of posting something half-broken."""
    cur = obj
    walked: list[str] = []
    for p in path:
        walked.append(str(p))
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                f"platform response is missing '{'.'.join(walked)}' (API changed?)",
                details={"cause": "SCHEMA_DRIFT", "missing_field": ".".join(walked)},
                retryable=False,
            ) from None
    if cur is None:
        raise ConnectorError(
            "PLATFORM_UNAVAILABLE",
            f"platform response has empty '{'.'.join(walked)}' (API changed?)",
            details={"cause": "SCHEMA_DRIFT", "missing_field": ".".join(walked)},
            retryable=False,
        )
    return cur
