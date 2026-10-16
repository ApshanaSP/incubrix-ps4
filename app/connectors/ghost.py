"""Ghost blogs through the Admin API (Ghost 5). Any site: config.base_url."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.connectors.base import Connection, Connector, Destination, Rendition, decode_post_id, encode_post_id
from app.connectors.http import require
from app.errors import ConnectorError


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def admin_jwt(admin_api_key: str) -> str:
    """Ghost Admin API token: HS256, kid = key id, secret = hex part, 5 minute life."""
    try:
        key_id, secret_hex = admin_api_key.split(":", 1)
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        raise ConnectorError("CONNECTION_AUTH_EXPIRED", "Ghost admin key must look like <id>:<hex secret>") from None
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": key_id}).encode())
    payload = _b64(json.dumps({"iat": now, "exp": now + 300, "aud": "/admin/"}).encode())
    sig = _b64(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def markdown_to_html(text: str) -> str:
    """Small, dependency-free conversion good enough for paragraphs, links and lists."""
    import html
    import re

    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for b in blocks:
        b_html = html.escape(b)
        b_html = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', b_html)
        b_html = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", b_html)
        if all(line.lstrip().startswith(("- ", "* ")) for line in b.splitlines()):
            items = "".join(f"<li>{line.lstrip()[2:]}</li>" for line in b_html.splitlines())
            out.append(f"<ul>{items}</ul>")
        elif b.startswith("#"):
            level = min(len(b) - len(b.lstrip("#")), 4)
            out.append(f"<h{level}>{b_html.lstrip('#').strip()}</h{level}>")
        else:
            out.append("<p>" + b_html.replace("\n", "<br>") + "</p>")
    return "\n".join(out)


class GhostConnector(Connector):
    key = "ghost"

    def _headers(self, connection: Connection) -> dict[str, str]:
        return {"Authorization": f"Ghost {admin_jwt(self.credential(connection, 'admin_api_key'))}",
                "Accept-Version": "v5.0"}

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        base = self.base_url(destination, connection)
        html_body = rendition.body if rendition.body_format == "html" else markdown_to_html(rendition.body)
        post: dict[str, Any] = {"title": rendition.title, "html": html_body,
                                "status": destination.config.get("status", "published"),
                                "tags": [{"name": t} for t in rendition.tags]}
        if rendition.canonical_url:
            post["canonical_url"] = rendition.canonical_url
        image = next((m.url for m in rendition.media if m.type == "image"), None)
        if image:
            post["feature_image"] = image
        resp = await self.http(base).request("POST", f"{base}/ghost/api/admin/posts/", params={"source": "html"},
                                             json={"posts": [post]}, headers=self._headers(connection))
        created = require(resp.json(), "posts", 0)
        return {"platform_post_id": encode_post_id(require(created, "id"), base), "live_url": require(created, "url"),
                "status": "live" if created.get("status") == "published" else created.get("status", "draft")}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        try:
            resp = await self.http(base).request("GET", f"{base}/ghost/api/admin/posts/{native}/",
                                                 headers=self._headers(connection))
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"status": "deleted", "live_url": None}
            raise
        post = require(resp.json(), "posts", 0)
        status = "live" if post.get("status") == "published" else post.get("status", "unknown")
        return {"status": status, "live_url": require(post, "url")}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        try:
            await self.http(base).request("DELETE", f"{base}/ghost/api/admin/posts/{native}/",
                                          headers=self._headers(connection))
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"deleted": True, "already_gone": True}
            raise
        return {"deleted": True}

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        base = self.base_url(destination, connection)
        resp = await self.http(base).request("GET", f"{base}/ghost/api/admin/posts/",
                                             params={"limit": 15, "order": "created_at desc"},
                                             headers=self._headers(connection))
        for p in require(resp.json(), "posts"):
            if p.get("title", "").strip() == rendition.title.strip():
                return {"platform_post_id": encode_post_id(p["id"], base), "live_url": p.get("url"), "status": "live"}
        return {}

    async def certify_probe(self, destination: Destination, connection: Connection) -> dict[str, Any]:
        base = self.base_url(destination, connection)
        resp = await self.http(base).request("GET", f"{base}/ghost/api/admin/site/", headers={"Accept-Version": "v5.0"})
        site = require(resp.json(), "site")
        return {"ok": True, "version": site.get("version")}
