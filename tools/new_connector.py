"""Make a new connector from the template, ready to certify on a mock platform in minutes.

    python -m tools.new_connector <key> --name "Platform name"

Writes three files and touches nothing else (the registry finds new connectors by itself):
  app/connectors/<key>.py                 the connector: the Appendix A methods over a small REST API
  app/connectors/manifests/<key>.yaml     its manifest; `mock: template_rest` serves it from the mock platforms
  tests/test_connector_<key>.py           certification, the contract round trip, an expired token,
                                          a timeout then retry, and a renamed API field
Then:
  python -m pytest tests/test_connector_<key>.py     # passes straight away, against the template mock
To connect the real platform: change the paths and field names in <key>.py to the real API (every field the
code relies on is read with require(), so a missing field fails certification as SCHEMA_DRIFT), set the
limits and error codes in the manifest, and remove the `mock:` line once a real mock or instance exists.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONNECTOR = '''"""{name} connector (made from the template by tools/new_connector.py).

The REST API it speaks (the template mock answers the same, see app/mocks/server.py: template_rest):
  GET    {{base}}/api/me                         certification probe
  POST   {{base}}/api/posts                      create; Idempotency-Key header
  GET    {{base}}/api/posts?idempotency_key=K    find a post after a timeout
  GET    {{base}}/api/posts/{{id}}                 status and stats
  DELETE {{base}}/api/posts/{{id}}                 delete
Change the paths and field names below to the real API. Fields the code relies on are read with require(),
so a renamed field fails loudly (SCHEMA_DRIFT) instead of silently.
"""
from __future__ import annotations

from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id, normalise_number)
from app.connectors.http import require
from app.errors import ConnectorError


class {cls}(Connector):
    key = "{key}"

    def _auth(self, connection: Connection) -> dict[str, str]:
        return {{"Authorization": f"Bearer {{self.credential(connection, 'api_key')}}"}}

    async def certify_probe(self, destination: Destination, connection: Connection) -> dict[str, Any]:
        base = self.base_url(destination, connection)
        data = (await self.http(base).request("GET", f"{{base}}/api/me", headers=self._auth(connection))).json()
        return {{"ok": bool(require(data, "ok"))}}

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        base = self.base_url(destination, connection)
        body = {{"title": rendition.title, "body": rendition.body, "link": rendition.link, "tags": rendition.tags}}
        headers = {{**self._auth(connection), "Idempotency-Key": idempotency_key}}
        data = (await self.http(base).request("POST", f"{{base}}/api/posts", json=body, headers=headers)).json()
        return {{"platform_post_id": encode_post_id(require(data, "id"), base), "live_url": require(data, "url"),
                "status": "live"}}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        try:
            data = (await self.http(base).request("GET", f"{{base}}/api/posts/{{native}}", headers=self._auth(connection))).json()
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {{"status": "deleted", "live_url": None}}
            raise
        return {{"status": data.get("status", "live"), "live_url": require(data, "url")}}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        try:
            await self.http(base).request("DELETE", f"{{base}}/api/posts/{{native}}", headers=self._auth(connection))
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {{"deleted": True, "already_gone": True}}
            raise
        return {{"deleted": True}}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        data = (await self.http(base).request("GET", f"{{base}}/api/posts/{{native}}", headers=self._auth(connection))).json()
        stats = data.get("stats") or {{}}
        out = empty_metrics()
        for name in out:
            out[name] = normalise_number(stats.get(name))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        """After a timeout: did the post get made? {{}} means no (safe to post), None would mean 'cannot tell'."""
        base = self.base_url(destination, connection)
        data = (await self.http(base).request("GET", f"{{base}}/api/posts", params={{"idempotency_key": idempotency_key}},
                                              headers=self._auth(connection))).json()
        posts = require(data, "posts")
        if posts:
            return {{"platform_post_id": encode_post_id(posts[0]["id"], base), "live_url": posts[0].get("url"),
                    "status": "live"}}
        return {{}}
'''

MANIFEST = '''key: {key}
version: 0.1.0
tier: native
auth: api_key
# served by the template REST mock until a real mock or instance exists (remove this line then)
mock: template_rest
surfaces:
  - key: post
    content_types: [text, image, video_link, link]
    spec: {{title_max_chars: 255, title_required: true, body_max_chars: 20000, links_allowed: true}}
rate_limits:
  per_connection: {{requests: 60, per_seconds: 60}}
capabilities: [publish, status, delete, metrics]
error_map:
  "429": PLATFORM_RATE_LIMIT
  "422": PLATFORM_CONTENT_REJECTED
  "400": PLATFORM_CONTENT_REJECTED
  "401": CONNECTION_AUTH_EXPIRED
  "403": CONNECTION_AUTH_EXPIRED
  "500": PLATFORM_UNAVAILABLE
  "502": PLATFORM_UNAVAILABLE
  "503": PLATFORM_UNAVAILABLE
instance_param: base_url
credentials: [api_key]
destination_config: {{base_url: "{name} root URL"}}
'''

TEST = '''"""{name} connector: made from the template; certifies and handles the hard cases on the mock platform."""
from __future__ import annotations

import uuid

from tests.conftest import wait_job

KEY = "{key}"


def _dest(client, **fields):
    host = f"{{KEY.replace('_', '-')}}-{{uuid.uuid4().hex[:6]}}.mock"
    con = client.post("/api/v1/connections", json={{"connector_key": KEY, "credentials": {{"api_key": "sim-key"}}}}).json()
    return client.post("/api/v1/destinations", json={{"url": f"https://{{host}}", "connector_key": KEY, "name": f"{name} {{host}}",
                                                     "config": {{"base_url": f"https://{{host}}"}}, "connection_id": con["id"],
                                                     "language": "en", **fields}}).json()


def _certify(client, dest):
    return wait_job(client, client.post(f"/api/v1/destinations/{{dest['id']}}/certify", json={{}}).json())["result"]


def _publish(client, dest, key=None):
    content = {{"title": "Template connector check", "body": "A short post to check the {name} connector."}}
    headers = {{"Idempotency-Key": key or uuid.uuid4().hex}}
    job = wait_job(client, client.post("/api/v1/publications", json={{"content": content, "destination_ids": [dest["id"]]}},
                                       headers=headers).json())
    return job["result"]["results"][0]


def test_manifest_has_every_appendix_a_field(client):
    m = client.get(f"/api/v1/connectors/{{KEY}}").json()
    for field in ("key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map", "instance_param"):
        assert field in m, field


def test_certifies_on_the_mock_platform(client):
    res = _certify(client, _dest(client))
    assert res["passed"] and res["level"] == "L4", res


def test_contract_round_trip_and_same_key(client):
    dest = _dest(client)
    _certify(client, dest)
    ref = {{"destination": {{"id": dest["id"]}}, "connection": {{"id": dest["connection_id"]}}}}
    rend = {{"title": "Contract round trip", "body": "Checking publish, status and delete."}}
    p1 = client.post(f"/connector/v1/{{KEY}}/publish", json={{**ref, "rendition": rend, "idempotency_key": "k1"}}).json()
    p2 = client.post(f"/connector/v1/{{KEY}}/publish", json={{**ref, "rendition": rend, "idempotency_key": "k1"}}).json()
    assert p1["platform_post_id"] == p2["platform_post_id"]
    status = client.post(f"/connector/v1/{{KEY}}/get_status", json={{"platform_post_id": p1["platform_post_id"],
                                                                    "connection": ref["connection"]}}).json()
    assert status["status"] == "live"
    assert client.post(f"/connector/v1/{{KEY}}/delete", json={{"platform_post_id": p1["platform_post_id"],
                                                             "connection": ref["connection"]}}).json()["deleted"]


def test_expired_token_pauses_the_destination(client):
    dest = _dest(client)
    _certify(client, dest)
    client.post("/api/v1/sim/faults", json={{"destination_id": dest["id"], "kind": "auth_expired", "op": "publish"}})
    assert _publish(client, dest)["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    assert client.get(f"/api/v1/destinations/{{dest['id']}}").json()["status"] == "paused"


def test_timeout_then_retry_makes_one_post(client):
    dest = _dest(client)
    _certify(client, dest)
    client.post("/api/v1/sim/faults", json={{"destination_id": dest["id"], "kind": "timeout_after_create", "op": "publish"}})
    first = _publish(client, dest, key="same-key")
    wait_job(client, client.post(f"/api/v1/posts/{{first['post_id']}}/retry", json={{}}).json())
    state = client.get("/api/v1/sim/state", params={{"destination_id": dest["id"]}}).json()
    assert state["posts_live"] == 1


def test_renamed_api_field_fails_certification(client):
    dest = _dest(client)
    _certify(client, dest)
    client.post("/api/v1/sim/faults", json={{"destination_id": dest["id"], "kind": "rename_field", "op": "any", "count": 5}})
    res = _certify(client, dest)
    assert not res["passed"] and res["error"]["details"]["cause"] == "SCHEMA_DRIFT"
    assert client.get(f"/api/v1/destinations/{{dest['id']}}").json()["status"] == "demoted"
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", help="lowercase connector key, for example 'circle' or 'discourse_like'")
    ap.add_argument("--name", help="display name, for example 'Circle communities'")
    ap.add_argument("--root", default=str(ROOT), help="project root (for tests of this tool)")
    args = ap.parse_args(argv)
    key = args.key.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", key):
        print("the key must be lowercase letters, digits or _, starting with a letter", file=sys.stderr)
        return 2
    root = Path(args.root)
    name = args.name or key.replace("_", " ").title()
    cls = "".join(part.capitalize() for part in key.split("_")) + "Connector"
    files = {
        root / "app" / "connectors" / f"{key}.py": CONNECTOR.format(key=key, name=name, cls=cls),
        root / "app" / "connectors" / "manifests" / f"{key}.yaml": MANIFEST.format(key=key, name=name),
        root / "tests" / f"test_connector_{key}.py": TEST.format(key=key, name=name),
    }
    taken = [str(f) for f in files if f.exists()]
    if taken:
        print("already exists, nothing written: " + ", ".join(taken), file=sys.stderr)
        return 1
    for f, text in files.items():
        f.write_text(text, encoding="utf-8")
        print("wrote", f.relative_to(root))
    print(f"\nnext:  python -m pytest tests/test_connector_{key}.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
