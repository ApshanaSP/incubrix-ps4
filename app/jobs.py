"""Background jobs (Integration Standard point 3).

Anything slower than 2 seconds runs as a job: the POST answers 202 with a
job_id; GET /api/v1/jobs/{job_id} gives status (queued, running, succeeded,
failed), progress 0-100 and result links. An optional callback URL receives
the finished job, signed with HMAC-SHA256 in X-IncuBrix-Signature.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from app.config import get_settings
from app.db import get_db, new_id, now_iso
from app.errors import ApiError
from app.logs import correlation_id, job_id as job_id_var, log

logger = logging.getLogger("jobs")

Progress = Callable[[int], None]
Runner = Callable[[Progress], Awaitable[dict[str, Any]]]

_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().job_workers)
    return _semaphore


def public_job(row: dict[str, Any]) -> dict[str, Any]:
    base = get_settings().public_base_url.rstrip("/")
    result = row.get("result") or None
    links = {"self": f"{base}/api/v1/jobs/{row['id']}"}
    if isinstance(result, dict):
        for k, v in (result.get("links") or {}).items():
            links[k] = v
    return {"job_id": row["id"], "kind": row["kind"], "status": row["status"], "progress": row["progress"],
            "result": result, "error": row.get("error") or None, "links": links,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def get_job(jid: str) -> dict[str, Any]:
    row = get_db().one("SELECT * FROM jobs WHERE id=?", [jid])
    if not row:
        raise ApiError("NOT_FOUND", f"job {jid} not found")
    return public_job(row)


def submit(kind: str, request: dict[str, Any], runner: Runner, callback_url: str | None = None) -> dict[str, Any]:
    jid = new_id("job")
    get_db().insert("jobs", {"id": jid, "kind": kind, "status": "queued", "progress": 0, "request": request,
                             "callback_url": callback_url, "correlation_id": correlation_id.get(),
                             "created_at": now_iso(), "updated_at": now_iso()})
    task = asyncio.get_running_loop().create_task(_run(jid, runner, callback_url))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return get_job(jid)


async def _run(jid: str, runner: Runner, callback_url: str | None) -> None:
    db = get_db()
    job_id_var.set(jid)
    async with _sem():
        db.update("jobs", "id", jid, {"status": "running", "progress": 1, "updated_at": now_iso()})
        log(logger, logging.INFO, "job started")
        last = {"p": 1}

        def progress(p: int) -> None:
            p = max(1, min(99, int(p)))
            if p != last["p"]:
                last["p"] = p
                db.update("jobs", "id", jid, {"progress": p, "updated_at": now_iso()})

        started = time.monotonic()
        try:
            result = await runner(progress)
            db.update("jobs", "id", jid, {"status": "succeeded", "progress": 100, "result": result,
                                          "updated_at": now_iso()})
            log(logger, logging.INFO, "job succeeded", seconds=round(time.monotonic() - started, 2))
        except ApiError as exc:
            db.update("jobs", "id", jid, {"status": "failed", "error": exc.to_dict()["error"], "updated_at": now_iso()})
            log(logger, logging.WARNING, "job failed", code=exc.code, error=exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("job crashed")
            db.update("jobs", "id", jid, {"status": "failed", "updated_at": now_iso(), "error": {
                "code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}", "retryable": True, "details": {}}})
    if callback_url:
        await send_callback(callback_url, get_job(jid))


def signature(body: bytes) -> str:
    secret = (get_settings().callback_secret or get_settings().secret_key or "local-dev-only-secret-change-me").encode()
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


async def send_callback(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, default=str).encode()
    headers = {"Content-Type": "application/json", "X-IncuBrix-Signature": signature(body),
               "X-Correlation-Id": correlation_id.get() or ""}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 500:
                log(logger, logging.INFO, "callback sent", status=resp.status_code)
                return
        except httpx.HTTPError as exc:
            log(logger, logging.WARNING, "callback failed", attempt=attempt + 1, error=type(exc).__name__)
        await asyncio.sleep(2 ** attempt)


def mark_interrupted() -> None:
    """Jobs that were running when the service stopped did not finish."""
    get_db().execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE status IN ('queued','running')",
                     [{"code": "INTERNAL_ERROR", "message": "service restarted while the job was running",
                       "retryable": True, "details": {}}, now_iso()])


async def wait_all(timeout: float = 120) -> None:
    """Used by tests and the batch runner."""
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=timeout)
