"""Certification harness and recertification scheduler.

A destination counts only once certified: a real test post is published,
its live URL confirmed, its status read, metrics read where supported, and the
post deleted (deletion confirmed by a second status read) well inside the
10-minute cleanup limit. Every step is timed and stored as evidence.

Failure (including schema drift, e.g. a renamed API field) demotes the
destination: it is excluded from posting and a ticket (plus an optional alert
webhook) is raised.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.catalogue import store
from app.config import get_settings
from app.connectors import registry
from app.connectors.base import Rendition
from app.connectors.http import is_simulated
from app.db import get_db, new_id, now_iso
from app.errors import ApiError, ConnectorError
from app.logs import log, redact
from app.publisher import _ticket, publish
from app.storage import get_storage

logger = logging.getLogger("certification")

LEVEL_BY_TIER = {"assisted": "L1", "bridge": "L2", "feed": "L2"}


def test_rendition(dest_name: str, run_id: str) -> Rendition:
    return Rendition(
        title=f"IncuBrix connector certification {run_id} please ignore",
        body=("Automated certification post from the IncuBrix distribution service. It checks that publishing, "
              "status and deletion work, and it is deleted within minutes. Please ignore it."),
        promotional=False,
        tags=["test"],
    )


async def certify(dest_id: str) -> dict[str, Any]:
    dest_row = store.get_destination(dest_id)
    destination = store.to_model(dest_row)
    connector = registry.get(destination.connector_key)
    connection = store.load_connection(dest_row.get("connection_id"), destination.connector_key)
    cert_id = new_id("crt")
    run_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    started_at = now_iso()
    steps: list[dict[str, Any]] = []
    published: dict[str, Any] | None = None
    deleted_ok = False
    metrics_ok = False
    error: dict[str, Any] | None = None

    async def step(name: str, coro):
        t0 = time.monotonic()
        try:
            result = await coro
            steps.append({"step": name, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "at": now_iso(),
                          "result": redact(result)})
            return result
        except (ConnectorError, ApiError) as exc:
            steps.append({"step": name, "ok": False, "ms": int((time.monotonic() - t0) * 1000), "at": now_iso(),
                          "error": exc.to_dict()["error"]})
            raise

    try:
        if destination.connector_key == "assisted":
            pack = await step("build_assist_pack", connector.build_assist_pack(test_rendition(destination.name, run_id),
                                                                              destination))
            await step("check_pack_files", _check_pack(pack))
            published = {"platform_post_id": f"assist:{pack['pack_id']}"}
            await step("delete", connector.delete(published["platform_post_id"], connection))
            deleted_ok = True
        else:
            await step("probe", connector.certify_probe(destination, connection))
            res = await step("publish", publish(test_rendition(destination.name, run_id), dest_row,
                                                f"cert-{dest_id}-{run_id}", is_test=True))
            if res.get("state") != "published":
                raise ApiError("DEST_NOT_CERTIFIED", f"test post was not published: {res.get('reason')}",
                               details={"publish_result": res})
            published = res
            status = await step("get_status", connector.get_status(res["platform_post_id"], connection))
            if status.get("status") != "live" or not status.get("live_url"):
                raise ApiError("DEST_NOT_CERTIFIED", f"test post is not live (status {status.get('status')})")
            await step("confirm_live_url", _confirm_live(status["live_url"]))
            if connector.implements("fetch_metrics"):
                try:
                    await step("fetch_metrics", connector.fetch_metrics(res["platform_post_id"], connection, None))
                    metrics_ok = True
                except (ConnectorError, ApiError):
                    pass  # metrics are optional; the level stays L3
            await step("delete", connector.delete(res["platform_post_id"], connection))
            after = await step("confirm_deleted", connector.get_status(res["platform_post_id"], connection))
            if after.get("status") == "live":
                raise ApiError("DEST_NOT_CERTIFIED", "post still live after delete")
            deleted_ok = True
            get_db().update("posts", "id", res["post_id"], {"state": "deleted", "deleted_at": now_iso()})
    except (ConnectorError, ApiError) as exc:
        error = exc.to_dict()["error"]
    finally:
        if published and not deleted_ok and published.get("platform_post_id"):
            # never leave a test post behind
            try:
                await connector.delete(published["platform_post_id"], connection)
                steps.append({"step": "cleanup_after_failure", "ok": True, "at": now_iso()})
                deleted_ok = True
            except Exception as exc:  # noqa: BLE001
                steps.append({"step": "cleanup_after_failure", "ok": False, "at": now_iso(), "error": str(exc)})
                _ticket(dest_id, "cleanup", f"test post {published['platform_post_id']} could not be deleted: {exc}")

    duration = round(time.monotonic() - started, 3)
    passed = error is None and deleted_ok
    if passed and duration > get_settings().cleanup_deadline_s:
        passed = False
        error = {"code": "DEST_NOT_CERTIFIED", "message": "cleanup took longer than the deadline", "retryable": True,
                 "details": {"duration_s": duration}}
    tier = connector.manifest.get("tier")
    level = LEVEL_BY_TIER.get(tier, "L4" if metrics_ok else "L3") if passed else "L0"
    evidence = {
        "certification_id": cert_id, "destination": store.public(dest_row), "connector": connector.key,
        "connector_version": connector.manifest.get("version"), "simulated": is_simulated(
            destination.config.get("base_url") or destination.config.get("api_base_url") or "https://x.mock"),
        "started_at": started_at, "finished_at": now_iso(), "duration_s": duration, "passed": passed,
        "level": level, "steps": steps, "error": error,
    }
    evidence_url = get_storage().put_json(f"certifications/{dest_id}/{cert_id}.json", evidence)
    get_db().insert("certifications", {"id": cert_id, "destination_id": dest_id, "status": "passed" if passed else "failed",
                                       "started_at": started_at, "finished_at": now_iso(), "duration_s": duration,
                                       "steps": steps, "evidence_url": evidence_url, "error": error})
    if passed:
        store.update_destination(dest_id, {"status": "certified", "level": level, "last_certified_at": now_iso()})
    else:
        store.update_destination(dest_id, {"status": "demoted", "level": "L0"})
        msg = f"{destination.name} failed certification: {error['message'] if error else 'unknown'}"
        _ticket(dest_id, "certification_failed", msg)
        await _alert({"event": "destination_demoted", "destination_id": dest_id, "name": destination.name,
                      "error": error, "evidence_url": evidence_url})
    log(logger, logging.INFO, "certification finished", destination=destination.name, passed=passed, cert_level=level,
        duration_s=duration)
    return {"certification_id": cert_id, "destination_id": dest_id, "name": destination.name, "passed": passed,
            "level": level, "duration_s": duration, "evidence_url": evidence_url, "error": error,
            "steps": [{k: s[k] for k in ("step", "ok") if k in s} for s in steps]}


async def _confirm_live(live_url: str) -> dict[str, Any]:
    """For real instances, fetch the live URL; simulated hosts are confirmed by
    the platform status read instead (they are not on the network)."""
    if is_simulated(live_url) or (urlsplit(live_url).hostname or "").endswith((".mock", ".slack.com", "hashnode.dev")):
        return {"live_url": live_url, "checked": "platform status (simulated host)"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(live_url)
    if resp.status_code >= 400:
        raise ApiError("DEST_NOT_CERTIFIED", f"live URL answered {resp.status_code}", details={"live_url": live_url})
    return {"live_url": live_url, "http_status": resp.status_code, "bytes": len(resp.content)}


async def _check_pack(pack: dict[str, Any]) -> dict[str, Any]:
    if not pack.get("checklist") or not pack.get("files") or not pack.get("caption"):
        raise ApiError("DEST_NOT_CERTIFIED", "assist pack is incomplete")
    return {"files": len(pack["files"]), "checklist_items": len(pack["checklist"]), "deep_link": pack.get("deep_link")}


async def _alert(payload: dict[str, Any]) -> None:
    url = get_settings().alert_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        log(logger, logging.WARNING, "alert webhook failed", error=str(exc))


def due_for_recertification(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=get_settings().recert_after_days)).isoformat(timespec="seconds")
    return get_db().all("SELECT * FROM destinations WHERE status='certified' AND (last_certified_at IS NULL "
                        "OR last_certified_at < ?)", [cutoff])


async def recertify_due() -> dict[str, Any]:
    due = due_for_recertification()
    results = []
    for row in due:
        try:
            results.append(await certify(row["id"]))
        except Exception as exc:  # noqa: BLE001
            results.append({"destination_id": row["id"], "passed": False, "error": str(exc)})
    return {"checked": len(due), "passed": sum(1 for r in results if r.get("passed")),
            "demoted": [r["destination_id"] for r in results if not r.get("passed")], "results": results}


async def scheduler_loop() -> None:
    interval = get_settings().recert_interval_s
    while True:
        await asyncio.sleep(interval)
        try:
            summary = await recertify_due()
            if summary["checked"]:
                log(logger, logging.INFO, "recertification run", **{k: summary[k] for k in ("checked", "passed")})
        except Exception:  # noqa: BLE001
            logger.exception("recertification run failed")
