"""Test setup: a fresh data dir, simulation mode, mock AI, short timeouts.
No API keys and no network are needed."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from typing import Any

import pytest

_TMP = tempfile.mkdtemp(prefix="ibx-tests-")
os.environ.update({
    "DATA_DIR": _TMP,
    "SIMULATION_MODE": "true",
    "SCHEDULER_ENABLED": "false",
    "AI_PROVIDER": "mock",
    "HTTP_TIMEOUT_S": "1.5",
    "MAX_BACKOFF_S": "5",
    "REACHABILITY_MODE": "offline",
    "SECRET_KEY": "test-secret-key",
    "API_TOKEN": "",
    "RATE_LIMIT_PER_MINUTE": "0",
    "LOG_LEVEL": "INFO",
    "PUBLIC_BASE_URL": "http://testserver",
    "S3_ENDPOINT": "",
    "MOCK_BASE_URL": "",
})

from fastapi.testclient import TestClient  # noqa: E402

from app.config import reload_settings  # noqa: E402

reload_settings()
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        # seeded demo destinations certify in the background at start-up
        deadline = time.time() + 60
        while time.time() < deadline:
            rows = c.get("/api/v1/destinations").json()["destinations"]
            if rows and all(r["status"] != "uncertified" for r in rows):
                break
            time.sleep(0.2)
        yield c
    shutil.rmtree(_TMP, ignore_errors=True)


def wait_job(client: TestClient, job: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    jid = job["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = client.get(f"/api/v1/jobs/{jid}").json()
        if cur["status"] in ("succeeded", "failed"):
            return cur
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish")


CREDS = {
    "discourse": {"api_key": "sim-discourse-key", "api_username": "incubrix_bot"},
    "lemmy": {"username": "incubrix_bot", "password": "sim-password"},
    "devto": {"api_key": "sim-devto-key"},
    "ghost": {"admin_api_key": "6489a6f2b8c1d2e3f4a5b6c7:" + "cd" * 32},
    "webhook": {"signing_secret": "whsec_simulation_only"},
    "hashnode": {"token": "sim-hashnode-token"},
    "slack": {"bot_token": "xoxb-sim-1111-test"},
}


def make_destination(client: TestClient, connector: str, certify: bool = True, **fields: Any) -> dict[str, Any]:
    """A fresh destination on its own mock instance: one catalogue row, no code."""
    host = f"{connector}-{uuid.uuid4().hex[:6]}.mock"
    config: dict[str, Any] = {
        "discourse": {"base_url": f"https://{host}"},
        "lemmy": {"base_url": f"https://{host}", "community": "incubrix_test"},
        "devto": {"api_base_url": f"https://{host}"},
        "ghost": {"base_url": f"https://{host}"},
        "webhook": {"base_url": f"https://{host}"},
        "hashnode": {"api_base_url": f"https://{host}", "publication_id": "pub1"},
        "slack": {"api_base_url": f"https://{host}", "channel": "C0TEST"},
        "mrss": {"feed_key": f"feed-{uuid.uuid4().hex[:6]}"},
        "assisted": {"submit_url": f"https://{host}/submit"},
    }[connector]
    config.update(fields.pop("config", {}))
    body: dict[str, Any] = {"url": f"https://{host}", "connector_key": connector, "name": f"test {connector} {host}",
                            "config": config, "audience_tags": ["ai", "creators"], "language": "en", **fields}
    if connector in CREDS:
        con = client.post("/api/v1/connections", json={"connector_key": connector, "credentials": CREDS[connector]}).json()
        body["connection_id"] = con["id"]
    dest = client.post("/api/v1/destinations", json=body).json()
    assert "id" in dest, dest
    if certify:
        job = wait_job(client, client.post(f"/api/v1/destinations/{dest['id']}/certify", json={}).json())
        assert job["status"] == "succeeded", job
        assert job["result"]["passed"], job["result"]
    return client.get(f"/api/v1/destinations/{dest['id']}").json()


def publish(client: TestClient, dest_ids: list[str], content: dict[str, Any] | None = None,
            key: str | None = None) -> dict[str, Any]:
    content = content or {"title": "How we cut video editing time with AI captions",
                          "body": "We tested three captioning workflows for short videos and measured the time each "
                                  "one saved. Here is what we learned.",
                          "tags": ["ai", "video"], "promotional": False}
    headers = {"Idempotency-Key": key} if key else {}
    job = client.post("/api/v1/publications", json={"content": content, "destination_ids": dest_ids},
                      headers=headers).json()
    return wait_job(client, job)


def sim_fault(client: TestClient, dest_id: str, kind: str, op: str = "publish", **kw: Any) -> None:
    r = client.post("/api/v1/sim/faults", json={"destination_id": dest_id, "kind": kind, "op": op, **kw})
    assert r.status_code == 200, r.text


def mock_live_posts(client: TestClient, dest_id: str) -> int:
    return client.get("/api/v1/sim/state", params={"destination_id": dest_id}).json()["posts_live"]
