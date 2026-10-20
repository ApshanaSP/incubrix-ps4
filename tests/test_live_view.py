"""The live view: a published post checked on its platform now (the platform's API, then its public page), and the
address a browser can open for hosts only the service reaches (Docker service names)."""
from __future__ import annotations

import httpx

from app import publisher
from app.config import Settings, get_settings
from tests.conftest import make_destination, publish, wait_job


def test_browser_url_rewrites_only_mapped_hosts(monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_url_map", {"http://ghost:2368": "http://localhost:2368"})
    assert publisher.browser_url("http://ghost:2368/hello/") == "http://localhost:2368/hello/"
    assert publisher.browser_url("http://ghost:2368") == "http://localhost:2368"
    assert publisher.browser_url("http://ghost:23680/x") == "http://ghost:23680/x"  # a whole host, not a prefix
    assert publisher.browser_url("https://example.com/p") == "https://example.com/p"
    assert publisher.browser_url(None) is None


def test_browser_url_map_setting(monkeypatch):
    monkeypatch.setenv("BROWSER_URL_MAP", "http://lemmy-ui:1234=http://localhost:1236/, http://ghost:2368=http://localhost:2368,junk")
    assert Settings().browser_url_map == {"http://lemmy-ui:1234": "http://localhost:1236",
                                          "http://ghost:2368": "http://localhost:2368"}
    monkeypatch.delenv("BROWSER_URL_MAP")
    assert Settings().browser_url_map == {}


def test_live_check_follows_a_post_from_live_to_deleted(client):
    dest = make_destination(client, "lemmy")
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "published", r
    live = client.get(f"/api/v1/posts/{r['post_id']}/live").json()
    assert live["platform"]["ok"] and live["platform"]["status"] == "live"
    assert live["live_url"] == r["live_url"]
    # a built-in test platform has no page on the network: the platform's answer is the evidence, nothing is framed
    assert live["simulated"] is True and live["page"] is None and live["embeddable"] is False

    assert wait_job(client, client.post(f"/api/v1/posts/{r['post_id']}/delete", json={}).json())["result"]["deleted"]
    gone = client.get(f"/api/v1/posts/{r['post_id']}/live").json()
    assert gone["state"] == "deleted" and gone["platform"]["status"] == "deleted"


def test_live_check_loads_the_page_and_respects_framing(client, monkeypatch):
    dest = make_destination(client, "ghost")
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "published", r
    monkeypatch.setattr(publisher, "is_simulated", lambda url: False)  # as for a real instance
    headers: dict[str, str] = {}
    seen: list[str] = []

    async def page(url: str) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, text="<html><head><title>Hello &amp; welcome</title></head></html>", headers=headers,
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(publisher, "_fetch_page", page)
    live = client.get(f"/api/v1/posts/{r['post_id']}/live").json()
    assert seen == [r["live_url"]]
    assert live["page"]["ok"] and live["page"]["http_status"] == 200 and live["page"]["title"] == "Hello & welcome"
    assert live["embeddable"] is True

    headers["X-Frame-Options"] = "DENY"  # the site forbids being shown inside another page
    live = client.get(f"/api/v1/posts/{r['post_id']}/live").json()
    assert live["page"]["ok"] and live["embeddable"] is False


def test_live_check_for_a_post_that_was_never_published(client):
    dest = make_destination(client, "discourse", self_promo_allowed="no")
    r = publish(client, [dest["id"]], {"title": "Try our tool free, sign up today", "body": "Use code LAUNCH50 for 50% off.",
                                       "promotional": True})["result"]["results"][0]
    live = client.get(f"/api/v1/posts/{r['post_id']}/live").json()
    assert live["state"] == "blocked" and live["platform"] is None and live["embeddable"] is False


def test_live_check_unknown_post(client):
    resp = client.get("/api/v1/posts/post_nope/live")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"
