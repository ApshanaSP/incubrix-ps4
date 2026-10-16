"""Lemmy communities (API v3, Lemmy 0.19). Any instance: config.base_url;
any community on it: config.community."""
from __future__ import annotations

import time
from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id, normalise_number)
from app.connectors.http import require
from app.errors import ConnectorError

# jwt cache: (base_url, username) -> (jwt, obtained_at). Lives in memory only.
_JWT: dict[tuple[str, str], tuple[str, float]] = {}
JWT_MAX_AGE_S = 6 * 3600


class LemmyConnector(Connector):
    key = "lemmy"

    async def _jwt(self, base: str, connection: Connection, force: bool = False) -> str:
        if connection.credentials.get("jwt"):
            return str(connection.credentials["jwt"])
        user = self.credential(connection, "username")
        cached = _JWT.get((base, user))
        if cached and not force and time.time() - cached[1] < JWT_MAX_AGE_S:
            return cached[0]
        try:
            resp = await self.http(base).request(
                "POST", f"{base}/api/v3/user/login",
                json={"username_or_email": user, "password": self.credential(connection, "password")},
            )
        except ConnectorError as exc:
            if exc.code == "PLATFORM_CONTENT_REJECTED":  # Lemmy answers 400 incorrect_login
                raise ConnectorError("CONNECTION_AUTH_EXPIRED", "Lemmy rejected the login; reconnect the account",
                                     details=exc.details) from None
            raise
        jwt = require(resp.json(), "jwt")
        _JWT[(base, user)] = (jwt, time.time())
        return jwt

    async def _authed(self, base: str, connection: Connection, method: str, url: str, **kw: Any):
        jwt = await self._jwt(base, connection)
        try:
            return await self.http(base).request(method, url, headers={"Authorization": f"Bearer {jwt}"}, **kw)
        except ConnectorError as exc:
            if exc.code != "CONNECTION_AUTH_EXPIRED" or connection.credentials.get("jwt"):
                raise
            jwt = await self._jwt(base, connection, force=True)  # session expired: log in once more
            return await self.http(base).request(method, url, headers={"Authorization": f"Bearer {jwt}"}, **kw)

    async def _community_id(self, base: str, destination: Destination) -> int:
        if destination.config.get("community_id"):
            return int(destination.config["community_id"])
        name = destination.config.get("community")
        if not name:
            raise ConnectorError("PLATFORM_UNAVAILABLE", "destination has no community configured", retryable=False,
                                 details={"cause": "MISSING_INSTANCE_PARAM", "param": "community"})
        resp = await self.http(base).request("GET", f"{base}/api/v3/community", params={"name": name})
        return int(require(resp.json(), "community_view", "community", "id"))

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        base = self.base_url(destination, connection)
        payload: dict[str, Any] = {"name": rendition.title, "community_id": await self._community_id(base, destination),
                                   "body": rendition.body or None}
        link = rendition.link or next((m.url for m in rendition.media if m.type in ("image", "video")), None)
        if link:
            payload["url"] = link
        if rendition.language:
            payload["language_id"] = destination.config.get("language_ids", {}).get(rendition.language[:2])
            if payload["language_id"] is None:
                payload.pop("language_id")
        resp = await self._authed(base, connection, "POST", f"{base}/api/v3/post", json=payload)
        post = require(resp.json(), "post_view", "post")
        pid = require(post, "id")
        return {"platform_post_id": encode_post_id(pid, base), "live_url": f"{base}/post/{pid}", "status": "live"}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        try:
            resp = await self._authed(base, connection, "GET", f"{base}/api/v3/post", params={"id": native})
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"status": "deleted", "live_url": None}
            raise
        post = require(resp.json(), "post_view", "post")
        if post.get("deleted"):
            return {"status": "deleted", "live_url": None}
        if post.get("removed"):
            return {"status": "removed_by_moderator", "live_url": None}
        return {"status": "live", "live_url": f"{base}/post/{native}"}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        resp = await self._authed(base, connection, "POST", f"{base}/api/v3/post/delete",
                                  json={"post_id": int(native), "deleted": True})
        return {"deleted": bool(require(resp.json(), "post_view", "post").get("deleted", True))}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        resp = await self._authed(base, connection, "GET", f"{base}/api/v3/post", params={"id": native})
        counts = require(resp.json(), "post_view", "counts")
        out = empty_metrics()
        out.update(likes=normalise_number(counts.get("upvotes", counts.get("score"))),
                   comments=normalise_number(counts.get("comments")))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        base = self.base_url(destination, connection)
        user = connection.credentials.get("username")
        if not user:
            return None
        resp = await self._authed(base, connection, "GET", f"{base}/api/v3/user",
                                  params={"username": user, "sort": "New", "limit": 20})
        for pv in require(resp.json(), "posts"):
            post = pv.get("post", {})
            if post.get("name", "").strip() == rendition.title.strip() and not post.get("deleted"):
                return {"platform_post_id": encode_post_id(post["id"], base), "live_url": f"{base}/post/{post['id']}",
                        "status": "live"}
        return {}

    async def certify_probe(self, destination: Destination, connection: Connection) -> dict[str, Any]:
        base = self.base_url(destination, connection)
        resp = await self.http(base).request("GET", f"{base}/api/v3/community",
                                             params={"name": destination.config.get("community", "")})
        view = require(resp.json(), "community_view")
        return {"ok": True, "members": (view.get("counts") or {}).get("subscribers")}
