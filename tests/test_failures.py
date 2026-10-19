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


def test_breaker_lets_one_trial_call_through_after_cooldown(client, monkeypatch):
    """The breaker stops calls after 5 failures, but not forever: after BREAKER_COOLDOWN_S one trial call is
    allowed, and its success closes the breaker again."""
    from app.config import get_settings

    dest = make_destination(client, "discourse")
    sim_fault(client, dest["id"], "server_error", op="publish", count=5)
    for i in range(5):
        publish(client, [dest["id"]], content={"title": f"Cooldown test number {i} title", "body": "Body text here."})
    tripped = publish(client, [dest["id"]], content={"title": "Cooldown test while open", "body": "Body text here."})
    assert tripped["result"]["results"][0]["error"]["details"].get("breaker_open") is True
    monkeypatch.setattr(get_settings(), "breaker_cooldown_s", 0)
    ok = publish(client, [dest["id"]], content={"title": "Cooldown test after the wait", "body": "Body text here."})
    assert ok["result"]["results"][0]["state"] == "published", ok
    monkeypatch.setattr(get_settings(), "breaker_cooldown_s", 300)
    again = publish(client, [dest["id"]], content={"title": "Cooldown test breaker closed", "body": "Body text here."})
    assert again["result"]["results"][0]["state"] == "published"


def test_daily_quota_used_up_is_quota_exhausted_and_rescheduled(client):
    dest = make_destination(client, "discourse")
    sim_fault(client, dest["id"], "quota_exhausted", retry_after=3600)
    t0 = time.time()
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert time.time() - t0 < 30  # no waiting on a daily quota
    assert r["error"]["code"] == "PLATFORM_QUOTA_EXHAUSTED" and r["error"]["retryable"] is True
    assert r["error"]["details"]["reset_at"]
    post = client.get(f"/api/v1/posts/{r['post_id']}").json()
    assert post["state"] == "rescheduled" and post["reschedule_at"]
    assert mock_live_posts(client, dest["id"]) == 0


def test_same_key_for_another_destination_is_a_conflict_not_a_replay(client):
    a = make_destination(client, "webhook")
    b = make_destination(client, "webhook")
    body = {"rendition": {"title": "One key, one destination", "body": "Body"}, "idempotency_key": "shared-key-1"}
    first = client.post("/connector/v1/webhook/publish", json={**body, "destination": {"id": a["id"]}})
    assert first.status_code == 200, first.text
    second = client.post("/connector/v1/webhook/publish", json={**body, "destination": {"id": b["id"]}})
    assert second.status_code == 409 and second.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert mock_live_posts(client, b["id"]) == 0


def test_connection_past_its_expiry_time_is_blocked_before_any_call(client):
    dest = make_destination(client, "devto")
    con_id = dest["connection_id"]
    client.patch(f"/api/v1/connections/{con_id}", json={"credentials": CREDS["devto"],
                                                         "expires_at": "2020-01-01T00:00:00+00:00"})
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "blocked" and r["code"] == "CONNECTION_AUTH_EXPIRED"
    assert client.get(f"/api/v1/connections/{con_id}").json()["status"] == "expired"
    assert client.get(f"/api/v1/destinations/{dest['id']}").json()["status"] == "paused"
    assert mock_live_posts(client, dest["id"]) == 0
    # a new token (no known expiry) resumes the destination
    client.patch(f"/api/v1/connections/{con_id}", json={"credentials": CREDS["devto"]})
    ok = publish(client, [dest["id"]], content={"title": "Back with a fresh token today", "body": "Body text."})
    assert ok["result"]["results"][0]["state"] == "published"


def test_retry_of_unknown_outcome_is_not_blocked_by_its_own_attempt(client):
    """A timed-out attempt whose outcome could not be checked stays in_flight. When the retry finds no post, it must
    publish: the post's own earlier attempt does not count against a 1-per-week cap."""
    dest = make_destination(client, "discourse", frequency_cap="1/7d")
    sim_fault(client, dest["id"], "timeout", op="publish")
    sim_fault(client, dest["id"], "server_error", op="list")  # the lookup after the timeout fails too
    first = publish(client, [dest["id"]], key="own-attempt-1")["result"]["results"][0]
    assert first["post_state"] == "in_flight", first
    retry = wait_job(client, client.post(f"/api/v1/posts/{first['post_id']}/retry", json={}).json())["result"]
    assert retry["state"] == "published", retry
    assert mock_live_posts(client, dest["id"]) == 1


def test_metrics_refresh_skips_posts_that_were_not_published(client):
    dest = make_destination(client, "discourse", self_promo_allowed="no")
    blocked = publish(client, [dest["id"]], content={"title": "Sign up now for our new tool", "body": "Sign up now.",
                                                     "promotional": True})["result"]["results"][0]
    job = wait_job(client, client.post("/api/v1/metrics/refresh", json={"post_ids": [blocked["post_id"]]}).json())
    assert job["status"] == "succeeded", job
    assert "skipped" in job["result"]["results"][0]
