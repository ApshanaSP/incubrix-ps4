"""Integration Standard checks and must-have 7 (secrets, simulation)."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging

import yaml

from app.jobs import signature
from tests.conftest import make_destination, publish, wait_job


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/ready").json()
    assert r["status"] == "ready" and r["checks"]["database"] == "ok"


def test_openapi_31_and_all_routes_under_api_v1(client):
    spec = yaml.safe_load(client.get("/openapi.yaml").text)
    assert spec["openapi"].startswith("3.1")
    for path in spec["paths"]:
        assert path.startswith(("/api/v1/", "/connector/v1/", "/health", "/ready")), path


def test_error_shape(client):
    r = client.get("/api/v1/destinations/dst_nope")
    assert r.status_code == 404
    err = r.json()["error"]
    assert set(err) == {"code", "message", "retryable", "details"} and err["code"] == "NOT_FOUND"
    r = client.post("/api/v1/destinations", json={"url": 5})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_job_lifecycle_and_correlation_id(client):
    r = client.post("/api/v1/certifications", json={"destination_ids": ["dst_mrss_ctv"]},
                    headers={"X-Correlation-Id": "corr-123"})
    assert r.status_code == 202 and r.headers["x-correlation-id"] == "corr-123"
    job = r.json()
    assert job["status"] in ("queued", "running", "succeeded") and "job_id" in job
    done = wait_job(client, job)
    assert done["status"] == "succeeded" and done["progress"] == 100
    assert "results" in done["links"] and done["result"]["manifest"]["kind"] == "certification_batch"


def test_idempotency_key_replays_same_job(client):
    body = {"content": {"title": "Idempotent publication request title", "body": "Body text"},
            "destination_ids": ["dst_mrss_ctv"], "dry_run": True}
    a = client.post("/api/v1/publications", json=body, headers={"Idempotency-Key": "same-key-1"})
    b = client.post("/api/v1/publications", json=body, headers={"Idempotency-Key": "same-key-1"})
    assert a.json()["job_id"] == b.json()["job_id"]
    assert b.headers.get("idempotent-replayed") == "true"
    c = client.post("/api/v1/publications", json={**body, "dry_run": False}, headers={"Idempotency-Key": "same-key-1"})
    assert c.status_code == 409 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_manifest_on_results(client):
    res = publish(client, ["dst_partner_news"], key="manifest-1")
    m = res["result"]["manifest"]
    for k in ("service", "connectors", "models", "settings", "seed", "timings", "cost_estimate_usd", "warnings", "rights"):
        assert k in m
    assert res["links"]["result"].startswith("http")


def test_files_in_upload_and_out_links(client):
    up = client.post("/api/v1/uploads", files={"file": ("clip.mp4", io.BytesIO(b"\x00" * 64), "video/mp4")}).json()
    path = up["url"].split("http://testserver")[1]
    got = client.get(path)
    assert got.status_code == 200 and len(got.content) == 64
    bad = client.get(path.replace("sig=", "sig=0"))
    assert bad.status_code == 404


def test_callback_signature(client):
    body = b'{"job_id":"x"}'
    expected = "sha256=" + hmac.new(b"test-secret-key", body, hashlib.sha256).hexdigest()
    assert signature(body) == expected


def test_secrets_never_in_logs_or_responses(client, caplog):
    caplog.set_level(logging.DEBUG)
    dest = make_destination(client, "discourse")
    publish(client, [dest["id"]], key="secret-scan")
    text = caplog.text + json.dumps(client.get("/api/v1/connections").json())
    for secret in ("sim-discourse-key", "sim-password", "whsec_simulation_only", "xoxb-sim", "sim-devto-key"):
        assert secret not in text, secret
    con = client.get(f"/api/v1/connections/{dest['connection_id']}").json()
    assert con["credentials"] == {"api_key": "<set>", "api_username": "<set>"}


def test_simulation_mode_needs_no_real_accounts(client):
    rows = client.get("/api/v1/destinations").json()["destinations"]
    assert len(rows) >= 12


def test_api_token_and_rate_limit(client, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "api_token", "t0ken")
    assert client.get("/api/v1/destinations").status_code == 401
    assert client.get("/api/v1/destinations", headers={"Authorization": "Bearer t0ken"}).status_code == 200
    assert client.get("/health").status_code == 200
    monkeypatch.setattr(s, "api_token", "")
    monkeypatch.setattr(s, "rate_limit_per_minute", 3)
    codes = [client.get("/api/v1/connectors", headers={"X-Forwarded-For": "9.9.9.9"}).status_code for _ in range(5)]
    assert codes[-1] == 429


def test_openapi_file_matches_running_service(client):
    from pathlib import Path

    on_disk = yaml.safe_load(Path(__file__).resolve().parent.parent.joinpath("openapi.yaml").read_text(encoding="utf-8"))
    served = yaml.safe_load(client.get("/openapi.yaml").text)
    assert on_disk == served, "openapi.yaml is stale: run python -m tools.export_openapi"


def test_dashboard_and_scenarios(client):
    page = client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 200 and "IncuBrix Long-Tail" in page.text and "<script>" in page.text
    assert client.get("/", headers={"Accept": "application/json"}).json()["docs"] == "/docs"
    names = [s["name"] for s in client.get("/ui/scenarios").json()["scenarios"]]
    assert len(names) == 17 and "02_timeout_then_retry_same_key" in names
    assert client.get("/ui/scenarios/02_timeout_then_retry_same_key").json()["steps"]
    assert client.get("/ui/files/messy_destinations.csv").status_code == 200
    for bad in ("/ui/scenarios/..%2Fpytest", "/ui/files/..%2F..%2Fapp%2Fconfig.py", "/ui/scenarios/nope"):
        r = client.get(bad)
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
