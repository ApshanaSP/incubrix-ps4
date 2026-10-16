"""Discourse forums (one connector, any forum: destination.config.base_url)."""
from __future__ import annotations

from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id, normalise_number)
from app.connectors.http import require
from app.errors import ConnectorError


class DiscourseConnector(Connector):
    key = "discourse"

    def _headers(self, connection: Connection) -> dict[str, str]:
        return {"Api-Key": self.credential(connection, "api_key"),
                "Api-Username": self.credential(connection, "api_username"),
                "Accept": "application/json"}

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        base = self.base_url(destination, connection)
        payload: dict[str, Any] = {"title": rendition.title, "raw": rendition.body}
        if destination.config.get("category_id"):
            payload["category"] = destination.config["category_id"]
        if rendition.tags and destination.config.get("tags_enabled"):
            payload["tags"] = rendition.tags
        resp = await self.http(base).request("POST", f"{base}/posts.json", json=payload, headers=self._headers(connection))
        data = resp.json()
        post_id = require(data, "id")
        topic_id = require(data, "topic_id")
        slug = require(data, "topic_slug")
        return {"platform_post_id": encode_post_id(post_id, base), "live_url": f"{base}/t/{slug}/{topic_id}",
                "status": "live"}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        try:
            resp = await self.http(base).request("GET", f"{base}/posts/{native}.json", headers=self._headers(connection))
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"status": "deleted", "live_url": None}
            raise
        data = resp.json()
        topic_id = require(data, "topic_id")
        slug = require(data, "topic_slug")
        if data.get("deleted_at"):
            status = "deleted"
        elif data.get("hidden"):
            status = "hidden"
        else:
            status = "live"
        return {"status": status, "live_url": f"{base}/t/{slug}/{topic_id}" if status == "live" else None}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        try:
            await self.http(base).request("DELETE", f"{base}/posts/{native}.json", headers=self._headers(connection))
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"deleted": True, "already_gone": True}
            raise
        return {"deleted": True}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        native, base = decode_post_id(platform_post_id, connection)
        base = self.base_url(None, Connection(base_url=base))
        http = self.http(base)
        post = (await http.request("GET", f"{base}/posts/{native}.json", headers=self._headers(connection))).json()
        topic_id = require(post, "topic_id")
        topic = (await http.request("GET", f"{base}/t/{topic_id}.json", headers=self._headers(connection))).json()
        out = empty_metrics()
        out.update(views=normalise_number(topic.get("views")), likes=normalise_number(topic.get("like_count")),
                   comments=normalise_number(topic.get("reply_count", max(0, normalise_number(topic.get("posts_count")) - 1))))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        base = self.base_url(destination, connection)
        user = self.credential(connection, "api_username")
        resp = await self.http(base).request("GET", f"{base}/topics/created-by/{user}.json", headers=self._headers(connection))
        topics = require(resp.json(), "topic_list", "topics")
        for t in topics:
            if t.get("title", "").strip() == rendition.title.strip():
                return {"platform_post_id": encode_post_id(t.get("first_post_id") or t["id"], base),
                        "live_url": f"{base}/t/{t.get('slug')}/{t['id']}", "status": "live"}
        return {}

    async def certify_probe(self, destination: Destination, connection: Connection) -> dict[str, Any]:
        base = self.base_url(destination, connection)
        about = (await self.http(base).request("GET", f"{base}/about.json")).json()
        stats = require(about, "about", "stats")
        return {"ok": True, "members": stats.get("users_count")}
