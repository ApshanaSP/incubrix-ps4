"""Must-have 1: every connector implements the contract methods, manifest and error codes (Appendix A)."""
from __future__ import annotations

import pytest

from app.connectors import registry
from app.errors import SHARED_CODES
from tests.conftest import CREDS, make_destination

REQUIRED_MANIFEST = {"key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map",
                     "instance_param"}


@pytest.mark.parametrize("key", registry.keys())
def test_manifest_shape(client, key):
    m = registry.get(key, check_kill_switch=False).manifest
    assert REQUIRED_MANIFEST <= set(m), REQUIRED_MANIFEST - set(m)
    assert m["key"] == key
    assert m["tier"] in ("native", "protocol", "bridge", "feed", "assisted")
    assert m["auth"] in ("oauth2", "api_key", "bot_token", "none")
    for s in m["surfaces"]:
        assert {"key", "content_types", "spec"} <= set(s)
    for code in m["error_map"].values():
        assert code in SHARED_CODES, f"{key} maps to non-contract code {code}"


@pytest.mark.parametrize("key", registry.keys())
def test_methods_implemented(client, key):
    c = registry.get(key, check_kill_switch=False)
    for method in ("publish", "get_status", "delete"):
        assert c.implements(method), f"{key} lacks {method}"
    if "metrics" in c.capabilities:
        assert c.implements("fetch_metrics")
    if c.manifest["tier"] == "assisted":
        assert c.implements("build_assist_pack")


@pytest.mark.parametrize("key", ["discourse", "lemmy", "devto", "ghost", "webhook", "hashnode", "slack", "mrss"])
def test_contract_http_round_trip(client, key):
    """publish -> get_status -> delete over POST /connector/v1/{key}/{method}, with inline destination/connection."""
    dest = make_destination(client, key, certify=False)
    connection = {"credentials": CREDS.get(key, {})}
    destination = {"name": dest["name"], "url": dest["url"], "config": dest["config"]}
    rendition = {"title": f"Contract round trip for the {key} connector", "body": "Body text long enough for every platform.",
                 "media": [{"url": "https://cdn.example.com/v.mp4", "type": "video", "mime": "video/mp4"}] if key == "mrss" else []}
    v = client.post(f"/connector/v1/{key}/validate", json={"rendition": rendition, "destination": destination,
                                                          "connection": connection}).json()
    assert v["ok"] is True, v
    pub = client.post(f"/connector/v1/{key}/publish", json={"rendition": rendition, "destination": destination,
                                                           "connection": connection, "idempotency_key": f"contract-{key}"})
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert set(body) >= {"platform_post_id", "live_url", "status"}
    st = client.post(f"/connector/v1/{key}/get_status", json={"platform_post_id": body["platform_post_id"],
                                                             "connection": connection}).json()
    assert st["status"] == "live" and st["live_url"]
    de = client.post(f"/connector/v1/{key}/delete", json={"platform_post_id": body["platform_post_id"],
                                                         "connection": connection}).json()
    assert de["deleted"] is True
    st2 = client.post(f"/connector/v1/{key}/get_status", json={"platform_post_id": body["platform_post_id"],
                                                              "connection": connection}).json()
    assert st2["status"] != "live"


def test_assisted_contract_pack(client):
    dest = {"name": "Some subreddit", "url": "https://www.reddit.com/r/test", "platform_key": "reddit",
            "self_promo_allowed": "no", "frequency_cap": "1/7d", "rules_url": "https://www.reddit.com/r/test/about/rules",
            "config": {"submit_url_template": "https://www.reddit.com/r/test/submit?title={title}&url={url}"}}
    res = client.post("/connector/v1/assisted/build_assist_pack", json={
        "rendition": {"title": "A short title", "body": "Body", "link": "https://incubrix.com/blog/x"},
        "destination": dest}).json()
    assert set(res) >= {"files", "caption", "deep_link", "checklist"}
    assert "title=A%20short%20title" in res["deep_link"]
    assert any("Self-promotion is NOT allowed" in c for c in res["checklist"])
    names = {f["name"] for f in res["files"]}
    assert {"post.md", "caption.txt", "checklist.md", "pack.zip"} <= names


def test_unknown_method_and_connector(client):
    r = client.post("/connector/v1/discourse/explode", json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "UNSUPPORTED_METHOD"
    r = client.post("/connector/v1/myspace/publish", json={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "UNKNOWN_CONNECTOR"


def test_kill_switch(client):
    client.post("/api/v1/connectors/hashnode/kill-switch", json={"disabled": True, "reason": "test"})
    r = client.post("/connector/v1/hashnode/get_status", json={"platform_post_id": "x", "connection": {}})
    assert r.json()["error"]["code"] == "CONNECTOR_DISABLED"
    client.post("/api/v1/connectors/hashnode/kill-switch", json={"disabled": False})
