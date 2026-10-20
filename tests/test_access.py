"""With API_TOKEN set the whole site needs the token: browsers are sent to /login and get a session cookie; API
clients send the token as a header. Health checks, signed file links and public feeds stay open."""
from __future__ import annotations

import pytest

from app import middleware
from app.config import get_settings

TOKEN = "demo-test-token-0123456789"
HTML = {"Accept": "text/html"}


@pytest.fixture
def locked(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_token", TOKEN)
    client.cookies.clear()
    yield client
    client.cookies.clear()
    middleware._failed_auth.clear()  # the wrong tokens tried here must not count against later tests


def test_every_page_needs_the_token(locked):
    for path in ("/", "/docs", "/ui/scenarios", "/ui/files/messy_destinations.csv"):
        r = locked.get(path, headers=HTML, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
    for path in ("/", "/openapi.yaml", "/openapi.json", "/api/v1/destinations"):
        r = locked.get(path, follow_redirects=False)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED", path
    # still open: health checks (the host's own probe) and the sign-in page itself
    assert locked.get("/health").status_code == 200
    assert locked.get("/ready").json()["checks"]["access"] == "token"
    page = locked.get("/login")
    assert page.status_code == 200 and 'action="/login"' in page.text and TOKEN not in page.text


def test_sign_in_sets_a_session_that_opens_everything(locked):
    r = locked.post("/login", data={"token": "wrong", "next": "/#pub"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error=wrong")
    assert middleware.SESSION_COOKIE not in r.cookies

    r = locked.post("/login", data={"token": TOKEN, "next": "/#pub"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#pub"
    cookie = r.cookies[middleware.SESSION_COOKIE]
    assert cookie and TOKEN not in cookie  # a value derived from the token, never the token
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "IncuBrix Long-Tail" in locked.get("/", headers=HTML).text
    assert locked.get("/api/v1/destinations").status_code == 200
    assert locked.get("/openapi.yaml").status_code == 200

    out = locked.get("/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    locked.cookies.clear()
    assert locked.get("/api/v1/destinations").status_code == 401


def test_header_token_still_works_and_a_changed_token_ends_sessions(locked, monkeypatch):
    assert locked.get("/api/v1/destinations", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert locked.get("/api/v1/destinations", headers={"X-API-Key": TOKEN}).status_code == 200
    locked.post("/login", data={"token": TOKEN})
    assert locked.get("/api/v1/destinations").status_code == 200
    monkeypatch.setattr(get_settings(), "api_token", TOKEN + "-rotated")
    assert locked.get("/api/v1/destinations").status_code == 401


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example/", "/\\evil.example", "javascript:alert(1)"])
def test_sign_in_never_redirects_off_site(locked, target):
    r = locked.post("/login", data={"token": TOKEN, "next": target}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_open_site_has_no_sign_in(client):
    assert not get_settings().api_token
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"
    assert client.get("/ready").json()["checks"]["access"] == "open"
