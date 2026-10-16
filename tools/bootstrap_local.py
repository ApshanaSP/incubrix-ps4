"""One-shot setup for `docker compose up` (runs in the `bootstrap` container).

1. Lemmy: log in as the configured admin, raise the local post rate limit, create
   the community `incubrix_test`.
2. Ghost: create the owner account (first run only), log in, create an
   "IncuBrix" custom integration and read its Admin API key.
3. Register both as destinations in the api (credentials go to the api only,
   where they are stored encrypted) and certify every destination.
Safe to run again: existing communities, owners and destinations are reused.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

API = os.getenv("API_URL", "http://localhost:8080")
LEMMY = os.getenv("LEMMY_URL", "http://localhost:8536")
GHOST = os.getenv("GHOST_URL", "http://localhost:2368")
API_HEADERS = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}


def say(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def wait_for(url: str, name: str, timeout: float = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code < 500:
                say(f"{name} is up")
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit(f"{name} did not come up at {url}")


# ---------------------------------------------------------------- Lemmy
def setup_lemmy() -> dict[str, Any]:
    wait_for(f"{LEMMY}/api/v3/site", "Lemmy")
    user, password = os.environ["LEMMY_ADMIN_USER"], os.environ["LEMMY_ADMIN_PASSWORD"]
    jwt = None
    for _ in range(20):  # the admin is created by Lemmy's first start; give it a moment
        r = httpx.post(f"{LEMMY}/api/v3/user/login", json={"username_or_email": user, "password": password}, timeout=15)
        if r.status_code == 200 and r.json().get("jwt"):
            jwt = r.json()["jwt"]
            break
        time.sleep(3)
    if not jwt:
        raise SystemExit("cannot log in to Lemmy as the admin from lemmy.hjson")
    auth = {"Authorization": f"Bearer {jwt}"}
    # local test instance: allow many test posts (certification runs post often)
    httpx.put(f"{LEMMY}/api/v3/site", headers=auth, timeout=15, json={
        "rate_limit_post": 1000, "rate_limit_post_per_second": 60, "rate_limit_message": 1000,
        "rate_limit_message_per_second": 60, "rate_limit_register": 100, "rate_limit_register_per_second": 3600})
    r = httpx.get(f"{LEMMY}/api/v3/community", params={"name": "incubrix_test"}, headers=auth, timeout=15)
    if r.status_code != 200:
        r = httpx.post(f"{LEMMY}/api/v3/community", headers=auth, timeout=15, json={
            "name": "incubrix_test", "title": "IncuBrix test community",
            "description": "Local test community for IncuBrix connector certification. Posts are deleted within minutes."})
        r.raise_for_status()
        say("created Lemmy community incubrix_test")
    community = r.json()["community_view"]["community"]
    return {"username": user, "password": password, "community": community["name"]}


# ---------------------------------------------------------------- Ghost
def setup_ghost() -> str:
    wait_for(f"{GHOST}/ghost/api/admin/site/", "Ghost", timeout=600)
    email, password = os.environ["GHOST_ADMIN_EMAIL"], os.environ["GHOST_ADMIN_PASSWORD"]
    headers = {"Origin": GHOST, "Accept-Version": "v5.0"}
    with httpx.Client(base_url=GHOST, headers=headers, timeout=30) as c:
        status = c.get("/ghost/api/admin/authentication/setup/").json()
        if not status["setup"][0]["status"]:
            r = c.post("/ghost/api/admin/authentication/setup/", json={"setup": [{
                "name": "IncuBrix Admin", "email": email, "password": password, "blogTitle": "IncuBrix local Ghost"}]})
            r.raise_for_status()
            say("created the Ghost owner account")
        r = c.post("/ghost/api/admin/session/", json={"username": email, "password": password})
        r.raise_for_status()
        existing = c.get("/ghost/api/admin/integrations/", params={"include": "api_keys"}).json().get("integrations", [])
        integ = next((i for i in existing if i["name"] == "IncuBrix"), None)
        if not integ:
            r = c.post("/ghost/api/admin/integrations/", params={"include": "api_keys,webhooks"},
                       json={"integrations": [{"name": "IncuBrix"}]})
            r.raise_for_status()
            integ = r.json()["integrations"][0]
            say("created the Ghost IncuBrix integration")
        key = next(k for k in integ["api_keys"] if k["type"] == "admin")
        # some Ghost versions return the full "<id>:<secret>" in secret
        return key["secret"] if ":" in key["secret"] else f"{key['id']}:{key['secret']}"


# ---------------------------------------------------------------- register in the api
def api(method: str, path: str, **kw: Any) -> httpx.Response:
    r = httpx.request(method, f"{API}{path}", headers=API_HEADERS, timeout=60, **kw)
    if r.status_code >= 400:
        say(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r


def register(connector: str, name: str, url: str, config: dict[str, Any], credentials: dict[str, Any],
             **fields: Any) -> str:
    existing = [d for d in api("GET", "/api/v1/destinations", params={"connector_key": connector}).json()["destinations"]
                if d["url"] == url]
    if existing:
        dest = existing[0]
        if dest.get("connection_id"):
            api("PATCH", f"/api/v1/connections/{dest['connection_id']}", json={"credentials": credentials})
        return dest["id"]
    con = api("POST", "/api/v1/connections", json={"connector_key": connector, "name": name,
                                                   "credentials": credentials}).json()
    body = {"name": name, "url": url, "connector_key": connector, "config": config, "connection_id": con["id"],
            "language": "en", "audience_tags": ["ai", "creators", "video"], **fields}
    return api("POST", "/api/v1/destinations", json=body).json()["id"]


def wait_job(job_id: str) -> dict[str, Any]:
    while True:
        job = api("GET", f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(1)


def main() -> int:
    wait_for(f"{API}/health", "api")
    ids = []
    try:
        lemmy = setup_lemmy()
        ids.append(register("lemmy", "Local Lemmy c/incubrix_test", f"{LEMMY}/c/{lemmy['community']}",
                            {"base_url": LEMMY, "community": lemmy["community"]},
                            {"username": lemmy["username"], "password": lemmy["password"]},
                            frequency_cap="5/1d", self_promo_allowed="limited", relevance_score=4,
                            relevance_reason="Local test community (real Lemmy 0.19)."))
    except Exception as exc:  # noqa: BLE001
        say(f"Lemmy setup failed: {exc}")
    try:
        key = setup_ghost()
        ids.append(register("ghost", "Local Ghost blog", GHOST, {"base_url": GHOST}, {"admin_api_key": key},
                            self_promo_allowed="yes", relevance_score=5, relevance_reason="Local Ghost 5 blog."))
    except Exception as exc:  # noqa: BLE001
        say(f"Ghost setup failed: {exc}")
    job = api("POST", "/api/v1/certifications", json={"all": True}).json()
    result = wait_job(job["job_id"])
    if result["status"] != "succeeded":
        say(f"certification job failed: {result.get('error')}")
        return 1
    for r in result["result"]["results"]:
        say(f"{'PASS' if r['passed'] else 'FAIL'} {r['level']:>3}  {r['name']}"
            + ("" if r["passed"] else f"  ({r['error']['message'] if r.get('error') else ''})"))
    say(f"done: {result['result']['summary']}; real local destinations: {ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
