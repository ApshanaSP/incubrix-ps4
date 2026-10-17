"""Must-haves 5 and 6; hard cases 1, 2 and 6 (partner revokes permission)."""
from __future__ import annotations

import time

from tests.conftest import CREDS, make_destination, mock_live_posts, publish, sim_fault, wait_job


def test_timeout_after_create_then_retry_makes_one_post(client):
    dest = make_destination(client, "discourse")
    sim_fault(client, dest["id"], "timeout_after_create")
    first = publish(client, [dest["id"]], key="timeout-retry-1")["result"]["results"][0]
    # the platform created the post but never answered; the service found it instead of guessing
    assert first["state"] == "published", first
    assert "recovered" in (first.get("reason") or "") or any("recovered" in w for w in first.get("warnings", []))
    # the same request again replays the same job
    again = publish(client, [dest["id"]], key="timeout-retry-1")["result"]["results"][0]
    assert again["platform_post_id"] == first["platform_post_id"]
    # an explicit retry of the post with its key returns the existing post
    retry = wait_job(client, client.post(f"/api/v1/posts/{first['post_id']}/retry", json={}).json())["result"]
    assert retry["platform_post_id"] == first["platform_post_id"] and retry.get("replayed") is True
    assert mock_live_posts(client, dest["id"]) == 1


def test_contract_publish_retry_same_key_after_timeout(client):
    dest = make_destination(client, "webhook", certify=False)
    destination = {"url": dest["url"], "config": dest["config"]}
    conn = {"credentials": CREDS["webhook"]}
    rendition = {"title": "Partner story about AI video tools", "body": "Body"}
    sim_fault(client, dest["id"], "timeout_after_create")
    r1 = client.post("/connector/v1/webhook/publish", json={"rendition": rendition, "destination": destination,
                                                           "connection": conn, "idempotency_key": "k-timeout"})
    r2 = client.post("/connector/v1/webhook/publish", json={"rendition": rendition, "destination": destination,
                                                           "connection": conn, "idempotency_key": "k-timeout"})
    assert r2.status_code == 200, r2.text
    if r1.status_code == 200:
        assert r1.json()["platform_post_id"] == r2.json()["platform_post_id"]
    assert mock_live_posts(client, dest["id"]) == 1


def test_timeout_before_create_is_reported_and_retry_posts_once(client):
    dest = make_destination(client, "lemmy")
    sim_fault(client, dest["id"], "timeout")
    first = publish(client, [dest["id"]], key="hang-1")["result"]["results"][0]
    assert first["state"] == "failed" and first["error"]["code"] == "PLATFORM_UNAVAILABLE"
    assert first["error"]["details"]["timeout"] is True
    second = wait_job(client, client.post(f"/api/v1/posts/{first['post_id']}/retry", json={}).json())["result"]
    assert second["state"] == "published"
    assert second["idempotency_key"] == "hang-1:" + dest["id"]
    assert mock_live_posts(client, dest["id"]) == 1


def test_429_short_reset_backs_off_then_succeeds(client):
    dest = make_destination(client, "slack")
    sim_fault(client, dest["id"], "rate_limit", retry_after=1)
    t0 = time.time()
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "published"
    assert time.time() - t0 >= 0.9  # waited for the platform's reset time


def test_429_long_reset_is_rescheduled_with_reset_time(client):
    dest = make_destination(client, "discourse")
    sim_fault(client, dest["id"], "rate_limit", retry_after=900)
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "failed"
    err = r["error"]
    assert err["code"] == "PLATFORM_RATE_LIMIT" and err["retryable"] is True
    assert err["details"]["retry_after_s"] >= 800 and err["details"]["reset_at"]
    post = client.get(f"/api/v1/posts/{client.get('/api/v1/posts', params={'destination_id': dest['id']}).json()['posts'][0]['id']}").json()
    assert post["state"] == "rescheduled" and post["reschedule_at"]


def test_expired_token_pauses_destination_until_reconnect(client):
    dest = make_destination(client, "ghost")
    sim_fault(client, dest["id"], "auth_expired")
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    d = client.get(f"/api/v1/destinations/{dest['id']}").json()
    assert d["status"] == "paused"
    con = client.get(f"/api/v1/connections/{d['connection_id']}").json()
    assert con["status"] == "expired"
    blocked = publish(client, [dest["id"]], content={"title": "Second try while paused here", "body": "x"})
    assert blocked["result"]["results"][0]["state"] == "blocked"
    # the owner reconnects
    client.patch(f"/api/v1/connections/{d['connection_id']}", json={"credentials": CREDS["ghost"]})
    assert client.get(f"/api/v1/destinations/{dest['id']}").json()["status"] == "certified"
    ok = publish(client, [dest["id"]], content={"title": "Back after reconnecting the account", "body": "x"})
    assert ok["result"]["results"][0]["state"] == "published"


def test_content_rejected_by_platform(client):
    dest = make_destination(client, "devto")
    sim_fault(client, dest["id"], "reject")
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert r["error"]["retryable"] is False
    assert "not allowed" in r["error"]["details"]["platform_message"]


def test_partner_revokes_permission(client):
    dest = make_destination(client, "webhook")
    sim_fault(client, dest["id"], "revoke")
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    d = client.get(f"/api/v1/destinations/{dest['id']}").json()
    assert d["status"] == "paused"
    assert client.get(f"/api/v1/connections/{d['connection_id']}").json()["status"] == "revoked"
    assert any(t["destination_id"] == dest["id"] for t in client.get("/api/v1/tickets").json()["tickets"])


def test_breaker_stops_after_five_consecutive_failures(client):
    dest = make_destination(client, "hashnode")
    host = dest["config"]["api_base_url"]
    sim_fault(client, dest["id"], "server_error", op="publish", count=10)
    for i in range(5):
        publish(client, [dest["id"]], content={"title": f"Breaker test {i} title here", "body": "x"})
    r = publish(client, [dest["id"]], content={"title": "Breaker test after trip", "body": "x"})["result"]["results"][0]
    assert r["error"]["details"].get("breaker_open") is True, (host, r)


def test_hard_delete_within_ten_minutes_on_certification(client):
    certs = client.get("/api/v1/certifications").json()["certifications"]
    passed = [c for c in certs if c["status"] == "passed"]
    assert passed and max(c["duration_s"] for c in passed) < 600


def test_redirected_or_html_api_answer_is_a_clear_error(monkeypatch):
    """A moved API (3xx) or a web page instead of JSON stops with a reason instead of crashing or retrying."""
    import asyncio

    import httpx

    from app.connectors import http as platform_http
    from app.errors import ConnectorError

    answers = iter([httpx.Response(301, headers={"location": "https://example.com/announcement"}),
                    httpx.Response(200, text="<html><body>Checking your browser</body></html>",
                                   headers={"content-type": "text/html"})])
    monkeypatch.setattr(platform_http, "_transport_for", lambda url: httpx.MockTransport(lambda req: next(answers)))
    client = platform_http.PlatformHTTP("hashnode", "redirect-check")
    for cause in ("API_MOVED", "NOT_AN_API_RESPONSE"):
        try:
            asyncio.run(client.request("POST", "https://gql.example.mock/", json={}))
            raise AssertionError("no error")
        except ConnectorError as exc:
            assert exc.code == "PLATFORM_UNAVAILABLE" and exc.retryable is False and exc.details["cause"] == cause
