"""Generic signed webhook for partner sites (protocol: docs/webhook-protocol.md).

Every request carries
  X-IncuBrix-Timestamp: unix seconds
  X-IncuBrix-Signature: sha256=HMAC_SHA256(signing_secret, "<timestamp>." + raw body)
  Idempotency-Key      (POST only; the partner must de-duplicate on it)
A partner that revokes access answers 403, which maps to CONNECTION_AUTH_EXPIRED.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id, normalise_number)
from app.connectors.http import require
from app.errors import ConnectorError


def sign(secret: str, timestamp: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()


class WebhookConnector(Connector):
    key = "webhook"

    def _signed(self, connection: Connection, body: dict[str, Any] | None) -> tuple[bytes, dict[str, str]]:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode() if body is not None else b""
        ts = str(int(time.time()))
        headers = {"X-IncuBrix-Timestamp": ts, "X-IncuBrix-Signature": sign(self.credential(connection, "signing_secret"), ts, raw)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return raw, headers

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        base = self.base_url(destination, connection)
        body = {
            "idempotency_key": idempotency_key,
            "title": rendition.title,
            "body": rendition.body,
            "body_format": rendition.body_format,
            "link": rendition.link,
            "canonical_url": rendition.canonical_url,
            "media": [m.model_dump(exclude_none=True) for m in rendition.media],
            "tags": rendition.tags,
            "ai_generated": rendition.ai_generated,
        }
        raw, headers = self._signed(connection, body)
        headers["Idempotency-Key"] = idempotency_key
        resp = await self.http(base).request("POST", f"{base}/incubrix/posts", content=raw, headers=headers)
        data = resp.json()
        return {"platform_post_id": encode_post_id(require(data, "id"), base), "live_url": require(data, "url"),
                "status": data.get("status", "live")}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        _, headers = self._signed(connection, None)
        try:
            resp = await self.http(base).request("GET", f"{base}/incubrix/posts/{native}", headers=headers)
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"status": "deleted", "live_url": None}
            raise
        data = resp.json()
        return {"status": data.get("status", "live"), "live_url": require(data, "url")}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        _, headers = self._signed(connection, None)
        try:
            await self.http(base).request("DELETE", f"{base}/incubrix/posts/{native}", headers=headers)
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"deleted": True, "already_gone": True}
            raise
        return {"deleted": True}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        _, headers = self._signed(connection, None)
        data = (await self.http(base).request("GET", f"{base}/incubrix/posts/{native}", headers=headers)).json()
        out = empty_metrics()
        for k in out:
            out[k] = normalise_number(data.get(k))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        base = self.base_url(destination, connection)
        _, headers = self._signed(connection, None)
        resp = await self.http(base).request("GET", f"{base}/incubrix/posts", params={"idempotency_key": idempotency_key},
                                             headers=headers)
        posts = require(resp.json(), "posts")
        if posts:
            p = posts[0]
            return {"platform_post_id": encode_post_id(p["id"], base), "live_url": p.get("url"), "status": "live"}
        return {}
