"""The guarded publish path every post goes through.

  1. idempotency ledger: a key that already produced a post returns that post
  2. destination must be certified (test posts during certification excepted)
  3. connection must be active (expired/revoked pauses the destination)
  4. format limits (connector.validate) and posting rules (rules.check)
     -> blocked or rescheduled with a reason; nothing is sent
  5. the attempt is written as `in_flight` BEFORE calling the platform
  6. publish; on a timeout the outcome is unknown, so we look for the post
     (connector.find_existing) before anyone may try again
A retry with the same idempotency key therefore never creates a second post,
even when the first call timed out after the platform had created it.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app import rules
from app.catalogue import store
from app.config import get_settings
from app.connectors import registry
from app.connectors.base import Connection, Destination, Rendition, collect_notes, fingerprint
from app.connectors.http import is_simulated
from app.db import get_db, new_id, now_iso
from app.errors import ApiError, ConnectorError
from app.logs import log

logger = logging.getLogger("publisher")
_key_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    if key not in _key_locks:
        _key_locks[key] = asyncio.Lock()
    return _key_locks[key]


def _post_result(post: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    out = {
        "post_id": post["id"], "destination_id": post["destination_id"], "state": post["state"],
        "platform_post_id": post.get("platform_post_id"), "live_url": post.get("live_url"),
        "reason": post.get("reason"), "reschedule_at": post.get("reschedule_at"), "idempotency_key": post["idempotency_key"],
    }
    if browser_url(post.get("live_url")) != post.get("live_url"):  # only on hosts a browser cannot reach (Docker names)
        out["browser_url"] = browser_url(post.get("live_url"))
    if replayed:
        out["replayed"] = True
    return out


async def publish(
    rendition: Rendition,
    dest_row: dict[str, Any],
    idempotency_key: str,
    *,
    is_test: bool = False,
    publication_id: str | None = None,
    extra_warnings: list[str] | None = None,
    connection: Connection | None = None,
    skip_cert_gate: bool = False,
) -> dict[str, Any]:
    """`connection` and `skip_cert_gate` are used by the contract endpoint, where
    IncuBrix's runtime passes the connection and decides certification itself."""
    async with _lock_for(idempotency_key):
        return await _publish_locked(rendition, dest_row, idempotency_key, is_test, publication_id,
                                     extra_warnings or [], connection, skip_cert_gate)


async def _publish_locked(rendition: Rendition, dest_row: dict[str, Any], key: str, is_test: bool,
                          publication_id: str | None, warnings: list[str], connection: Connection | None,
                          skip_cert_gate: bool) -> dict[str, Any]:
    db = get_db()
    destination = store.to_model(dest_row)
    connector = registry.get(destination.connector_key)
    existing = db.one("SELECT * FROM posts WHERE idempotency_key=?", [key])

    # 1. replay (a key belongs to one destination: reusing it elsewhere must not return another destination's post)
    if existing and existing["destination_id"] != destination.id:
        raise ApiError("IDEMPOTENCY_KEY_CONFLICT", "this idempotency key was used for another destination",
                       details={"post_id": existing["id"], "destination_id": existing["destination_id"]})
    if existing and existing["state"] in ("published", "awaiting_human", "deleted"):
        log(logger, logging.INFO, "idempotent replay", idempotency_key=key, post_id=existing["id"])
        return _post_result(existing, replayed=True)

    if connection is None:
        connection = store.load_connection(dest_row.get("connection_id"), destination.connector_key)

    # 1b. an earlier attempt's outcome is unknown: find out before posting again
    if existing and existing["state"] == "in_flight":
        found = await _reconcile(connector, rendition, destination, connection, key)
        if found:
            db.update("posts", "id", existing["id"], {"state": "published", "platform_post_id": found["platform_post_id"],
                                                     "live_url": found.get("live_url"), "published_at": now_iso(),
                                                     "reason": "recovered after an unanswered call; no second post made"})
            return _post_result(db.one("SELECT * FROM posts WHERE id=?", [existing["id"]]), replayed=True)
        if found is None:
            raise ApiError("PLATFORM_UNAVAILABLE", "the earlier attempt's outcome is still unknown; not posting blind",
                           details={"post_id": existing["id"], "outcome_unknown": True}, retryable=True)

    # 2. certification gate
    status = dest_row.get("status")
    if not is_test and not skip_cert_gate and status != "certified":
        reason = {"demoted": "destination failed recertification and is excluded",
                  "paused": "destination is paused (connection expired or permission revoked)",
                  "excluded": "destination is excluded"}.get(status or "", "destination is not certified yet")
        return _record(existing, destination, key, rendition, "blocked", reason, is_test, publication_id,
                       code="DEST_NOT_CERTIFIED")

    # 3. connection
    if connection.status == "active" and _past(connection.expires_at):
        # the token's own expiry time has passed: do not spend a platform call to learn that
        if connection.id:
            store.set_connection_status(connection.id, "expired")
        if not is_test and destination.id and db.one("SELECT id FROM destinations WHERE id=?", [destination.id]):
            store.update_destination(destination.id, {"status": "paused"})
            _ticket(destination.id, "connection", f"{destination.name}: the connection's token expired at "
                                                  f"{connection.expires_at}. Ask the owner to reconnect.")
        connection = connection.model_copy(update={"status": "expired"})
    if connection.status in ("expired", "revoked"):
        return _record(existing, destination, key, rendition, "blocked",
                       f"connection is {connection.status}; the owner must reconnect", is_test, publication_id,
                       code="CONNECTION_AUTH_EXPIRED")

    # 4. format + rules (this post's own earlier attempt does not count against the frequency cap)
    own_id = existing["id"] if existing else None
    spec_check = await connector.validate(rendition, destination, connection)
    rule_check = rules.check(rendition, destination, use_ledger=not is_test,
                             native=destination.connector_key != "assisted", rules_status=dest_row.get("rules_status"),
                             exclude_post_id=own_id)
    hard = [v for v in rule_check["violations"] if v["action"] != "reschedule"]
    if not is_test and destination.connector_key != "assisted" and hard and all(v["rule"] == "automation" for v in hard):
        # automation is not allowed here: no bot posts. A person gets the ready-to-post pack instead, and every
        # other rule (caps, promotion, links, disclosure, language) is still checked for that pack.
        connector = registry.get("assisted")
        spec_check = await connector.validate(rendition, destination, connection)
        rule_check = rules.check(rendition, destination, use_ledger=True, native=False,
                                 rules_status=dest_row.get("rules_status"), exclude_post_id=own_id)
        warnings = warnings + ["automated posting is not allowed here, so nothing was posted by a bot: "
                               "a ready-to-post pack was made for a person to post (assisted)"]
    violations = spec_check["violations"] + rule_check["violations"]
    warnings = warnings + rule_check["warnings"]
    if violations:
        reschedules = [v for v in violations if v["action"] == "reschedule"]
        blocks = [v for v in violations if v["action"] != "reschedule"]
        state = "blocked" if blocks else "rescheduled"
        reason = "; ".join(v["message"] for v in (blocks or reschedules))
        when = max((v["reschedule_at"] for v in reschedules), default=None) if not blocks else None
        res = _record(existing, destination, key, rendition, state, reason, is_test, publication_id,
                      code=(blocks or reschedules)[0]["code"], reschedule_at=when)
        res["violations"] = violations
        res["warnings"] = warnings
        return res

    # 5. write the attempt before calling out
    post_id = existing["id"] if existing else new_id("pst")
    row = {"destination_id": destination.id, "connector_key": connector.key, "state": "in_flight",
           "is_test": int(is_test), "promotional": int(rule_check["promotional"]), "title": rendition.title,
           "fingerprint": fingerprint(rendition.title, rendition.body), "rendition": rendition.model_dump(exclude_none=True),
           "publication_id": publication_id, "reason": None, "reschedule_at": None}
    if existing:
        db.update("posts", "id", post_id, {**row, "attempts": (existing.get("attempts") or 0) + 1})
    else:
        db.insert("posts", {"id": post_id, "idempotency_key": key, "attempts": 1, "created_at": now_iso(), **row})

    # 6. publish
    call_notes = collect_notes()
    try:
        result = await connector.publish(rendition, destination, connection, key)
    except ConnectorError as exc:
        return await _handle_error(exc, connector, rendition, destination, connection, dest_row, key, post_id, warnings)
    state = "awaiting_human" if result.get("status") == "awaiting_human" else "published"
    db.update("posts", "id", post_id, {"state": state, "platform_post_id": result["platform_post_id"],
                                       "live_url": result.get("live_url"), "published_at": now_iso()})
    out = _post_result(db.one("SELECT * FROM posts WHERE id=?", [post_id]))
    out["status"] = result.get("status")
    if result.get("assist_pack"):
        out["assist_pack"] = result["assist_pack"]
    out["warnings"] = warnings + call_notes
    log(logger, logging.INFO, "published", destination=destination.name, post_id=post_id, live_url=result.get("live_url"),
        test=is_test)
    return out


async def _reconcile(connector, rendition: Rendition, destination: Destination, connection: Connection,
                     key: str) -> dict[str, Any] | None:
    try:
        return await connector.find_existing(rendition, destination, connection, key)
    except ConnectorError as exc:
        log(logger, logging.WARNING, "reconcile lookup failed", error=exc.code)
        return None


async def _handle_error(exc: ConnectorError, connector, rendition: Rendition, destination: Destination,
                        connection: Connection, dest_row: dict[str, Any], key: str, post_id: str,
                        warnings: list[str]) -> dict[str, Any]:
    db = get_db()
    details = exc.details or {}
    if details.get("timeout") and details.get("outcome_unknown"):
        # the platform may have created the post; look before anything else
        found = await _reconcile(connector, rendition, destination, connection, key)
        if found:
            db.update("posts", "id", post_id, {"state": "published", "platform_post_id": found["platform_post_id"],
                                               "live_url": found.get("live_url"), "published_at": now_iso(),
                                               "reason": "call timed out but the post was created; recovered it"})
            out = _post_result(db.one("SELECT * FROM posts WHERE id=?", [post_id]))
            out["warnings"] = warnings + ["publish call timed out; the post exists and was recovered (no duplicate)"]
            return out
        if found == {}:
            db.update("posts", "id", post_id, {"state": "failed", "reason": "timed out before the platform created it"})
        else:
            db.update("posts", "id", post_id, {"reason": "timed out; outcome unknown (stays in_flight until checked)"})
        raise exc
    if exc.code == "CONNECTION_AUTH_EXPIRED":
        if connection.id:
            revoked = details.get("http_status") == 403 or "revoked" in str(details.get("platform_message", ""))
            store.set_connection_status(connection.id, "revoked" if revoked else "expired")
        if destination.id and get_db().one("SELECT id FROM destinations WHERE id=?", [destination.id]):
            store.update_destination(destination.id, {"status": "paused"})
        _ticket(destination.id, "connection", f"{destination.name}: {exc.message}. Ask the owner to reconnect.")
        db.update("posts", "id", post_id, {"state": "blocked", "reason": exc.message})
        raise exc
    if exc.code in ("PLATFORM_RATE_LIMIT", "PLATFORM_QUOTA_EXHAUSTED"):
        when = details.get("reset_at")
        db.update("posts", "id", post_id, {"state": "rescheduled", "reason": exc.message, "reschedule_at": when})
        raise exc
    db.update("posts", "id", post_id, {"state": "failed", "reason": exc.message})
    if details.get("cause") == "SCHEMA_DRIFT":
        # the post may exist even though we could not read the answer; clean it up if we can find it
        found = await _reconcile(connector, rendition, destination, connection, key)
        if found and found.get("platform_post_id"):
            try:
                await connector.delete(found["platform_post_id"], connection)
                exc.details = {**details, "orphan_cleaned_up": found["platform_post_id"]}
            except ConnectorError:
                exc.details = {**details, "orphan_left": found["platform_post_id"]}
    raise exc


def _record(existing: dict[str, Any] | None, destination: Destination, key: str, rendition: Rendition, state: str,
            reason: str, is_test: bool, publication_id: str | None, code: str,
            reschedule_at: str | None = None) -> dict[str, Any]:
    db = get_db()
    row = {"destination_id": destination.id, "connector_key": destination.connector_key, "state": state,
           "reason": reason, "reschedule_at": reschedule_at, "is_test": int(is_test), "title": rendition.title,
           "rendition": rendition.model_dump(exclude_none=True), "publication_id": publication_id}
    if existing:
        db.update("posts", "id", existing["id"], row)
        pid = existing["id"]
    else:
        pid = new_id("pst")
        db.insert("posts", {"id": pid, "idempotency_key": key, "created_at": now_iso(), "attempts": 0, **row})
    out = _post_result(db.one("SELECT * FROM posts WHERE id=?", [pid]))
    out["code"] = code
    return out


def _ticket(destination_id: str | None, kind: str, message: str) -> None:
    get_db().insert("tickets", {"id": new_id("tkt"), "destination_id": destination_id, "kind": kind, "message": message,
                                "status": "open", "created_at": now_iso()})


async def delete_post(post_id: str) -> dict[str, Any]:
    db = get_db()
    post = db.one("SELECT * FROM posts WHERE id=?", [post_id])
    if not post:
        raise ApiError("NOT_FOUND", f"post {post_id} not found")
    if post["state"] not in ("published", "awaiting_human") or not post.get("platform_post_id"):
        return {"post_id": post_id, "deleted": False, "state": post["state"], "reason": "nothing was published"}
    dest_row = store.get_destination(post["destination_id"])
    connection = store.load_connection(dest_row.get("connection_id"), post["connector_key"])
    result = await registry.get(post["connector_key"]).delete(post["platform_post_id"], connection)
    if result.get("deleted"):
        db.update("posts", "id", post_id, {"state": "deleted", "deleted_at": now_iso()})
    return {"post_id": post_id, **result}


def browser_url(url: str | None) -> str | None:
    """The address a person's browser can open: BROWSER_URL_MAP rewrites hosts only the service can reach
    (http://ghost:2368 inside Docker -> http://localhost:2368)."""
    if not url:
        return url
    for inner, outer in get_settings().browser_url_map.items():
        if url == inner or url.startswith(inner + "/"):
            return outer + url[len(inner):]
    return url


async def _fetch_page(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        return await client.get(url)


async def live_check(post_id: str) -> dict[str, Any]:
    """Is this post on the platform right now? Asks the platform's API for the post, then loads its public page,
    timing both. Nothing is written: it is the same evidence certification collects, for any published post."""
    post = get_db().one("SELECT * FROM posts WHERE id=?", [post_id])
    if not post:
        raise ApiError("NOT_FOUND", f"post {post_id} not found")
    out: dict[str, Any] = {"post_id": post_id, "connector_key": post["connector_key"], "state": post["state"],
                           "title": post.get("title"), "published_at": post.get("published_at"),
                           "live_url": post.get("live_url"), "browser_url": browser_url(post.get("live_url")),
                           "simulated": bool(post.get("live_url")) and is_simulated(post["live_url"]),
                           "checked_at": now_iso(), "platform": None, "page": None, "embeddable": False}
    if not post.get("platform_post_id") or post["state"] not in ("published", "deleted"):
        return out
    dest_row = store.get_destination(post["destination_id"])
    connection = store.load_connection(dest_row.get("connection_id"), post["connector_key"])
    started = time.monotonic()
    try:
        status = await registry.get(post["connector_key"], check_kill_switch=False).get_status(
            post["platform_post_id"], connection)
        out["platform"] = {"ok": True, "status": status.get("status"), "ms": round(1000 * (time.monotonic() - started))}
    except (ConnectorError, ApiError) as exc:
        out["platform"] = {"ok": False, "error": exc.to_dict()["error"],
                           "ms": round(1000 * (time.monotonic() - started))}
        return out
    live_url = status.get("live_url") or post.get("live_url")
    if status.get("status") != "live" or not live_url or out["simulated"]:
        return out
    out["live_url"], out["browser_url"] = live_url, browser_url(live_url)
    started = time.monotonic()
    try:
        resp = await _fetch_page(live_url)
        title =re.search(r"<title[^>]*>(.*?)</title>", resp.text[:200_000], re.I | re.S)
        out["page"] = {"ok": resp.status_code < 400, "http_status": resp.status_code, "bytes": len(resp.content),
                       "title": html.unescape(title.group(1)).strip()[:200] if title else None,
                       "ms": round(1000 * (time.monotonic() - started))}
        # a site that forbids being framed would show an empty box, so say so instead
        csp = resp.headers.get("content-security-policy", "")
        framing_blocked = bool(resp.headers.get("x-frame-options")) or "frame-ancestors" in csp
    except httpx.HTTPError as exc:
        out["page"] = {"ok": False, "error": type(exc).__name__, "ms": round(1000 * (time.monotonic() - started))}
        framing_blocked = False
    out["embeddable"] = bool(out["page"]["ok"]) and not framing_blocked
    return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _past(when: str | None) -> bool:
    """True when an ISO time (a connection's expires_at) has passed. Unreadable values count as not passed."""
    if not when:
        return False
    try:
        t = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t <= utcnow()
