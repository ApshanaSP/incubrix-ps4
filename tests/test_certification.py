"""Must-haves 2 and 3, Core certification, recertification and schema drift."""
from __future__ import annotations

import json

from app.db import get_db
from app.storage import get_storage
from tests.conftest import make_destination, publish, sim_fault, wait_job

CORE = {"discourse", "lemmy", "devto", "ghost", "webhook", "assisted"}


def test_seeded_core_connectors_certified(client):
    rows = client.get("/api/v1/destinations").json()["destinations"]
    certified = {r["connector_key"] for r in rows if r["status"] == "certified"}
    assert CORE <= certified, CORE - certified
    assert {"hashnode", "slack", "mrss"} <= certified


def test_certification_evidence_has_every_step(client):
    dest = make_destination(client, "discourse")
    cert = client.get("/api/v1/certifications", params={"destination_id": dest["id"]}).json()["certifications"][0]
    steps = [s["step"] for s in cert["steps"]]
    assert steps[:6] == ["probe", "publish", "get_status", "confirm_live_url", "fetch_metrics", "delete"]
    assert "confirm_deleted" in steps
    assert cert["status"] == "passed" and cert["duration_s"] < 600
    key = cert["evidence_url"].split("/api/v1/files/")[1].split("?")[0]
    evidence = json.loads(get_storage().get_bytes(key))
    assert evidence["passed"] and evidence["level"] == "L4"
    # the test post is gone on the platform
    assert client.get("/api/v1/sim/state", params={"destination_id": dest["id"]}).json()["posts_live"] == 0


def test_new_instance_is_one_row(client):
    """'We add a new instance during review': a new forum of a supported type is one row, no code."""
    dest = make_destination(client, "discourse")
    assert dest["status"] == "certified"
    res = publish(client, [dest["id"]], key="one-row-test")
    assert res["result"]["results"][0]["state"] == "published"


def test_uncertified_destination_is_not_used(client):
    dest = make_destination(client, "lemmy", certify=False)
    res = publish(client, [dest["id"]])
    r = res["result"]["results"][0]
    assert r["state"] == "blocked" and r["code"] == "DEST_NOT_CERTIFIED"


def test_renamed_api_field_fails_certification_and_demotes(client):
    dest = make_destination(client, "devto")
    sim_fault(client, dest["id"], "rename_field", op="publish")
    job = wait_job(client, client.post(f"/api/v1/destinations/{dest['id']}/certify", json={}).json())
    res = job["result"]
    assert res["passed"] is False
    assert res["error"]["details"]["cause"] == "SCHEMA_DRIFT"
    after = client.get(f"/api/v1/destinations/{dest['id']}").json()
    assert after["status"] == "demoted" and after["level"] == "L0"
    tickets = client.get("/api/v1/tickets").json()["tickets"]
    assert any(t["destination_id"] == dest["id"] and t["kind"] == "certification_failed" for t in tickets)
    # demoted destinations are excluded from posting
    out = publish(client, [dest["id"]])["result"]["results"][0]
    assert out["state"] == "blocked" and out["code"] == "DEST_NOT_CERTIFIED"
    # the orphan created before the drift was detected is cleaned up
    assert client.get("/api/v1/sim/state", params={"destination_id": dest["id"]}).json()["posts_live"] == 0


def test_recertification_scheduler_demotes_failing(client):
    ok = make_destination(client, "ghost")
    bad = make_destination(client, "slack")
    get_db().execute("UPDATE destinations SET last_certified_at='2026-01-01T00:00:00+00:00' WHERE id IN (?,?)",
                     [ok["id"], bad["id"]])
    sim_fault(client, bad["id"], "rename_field", op="status", count=3)
    job = wait_job(client, client.post("/api/v1/recertification/run", json={}).json())
    assert bad["id"] in job["result"]["demoted"]
    assert client.get(f"/api/v1/destinations/{ok['id']}").json()["status"] == "certified"
    assert client.get(f"/api/v1/destinations/{bad['id']}").json()["status"] == "demoted"


def test_levels_by_tier(client):
    rows = {r["id"]: r for r in client.get("/api/v1/destinations").json()["destinations"]}
    assert rows["dst_assist_hn"]["level"] == "L1"
    assert rows["dst_mrss_ctv"]["level"] == "L2"
    assert rows["dst_partner_news"]["level"] == "L2"
    assert rows["dst_forum_aicreators"]["level"] == "L4"
