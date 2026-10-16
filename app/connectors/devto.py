"""Dev.to and any other Forem instance (config.api_base_url, default https://dev.to).

The Forem API has no delete endpoint. `delete` unpublishes the article, which
removes it from every public page and feed; the report says so plainly.
"""
from __future__ import annotations

from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id, normalise_number)
from app.connectors.http import require
from app.errors import ConnectorError

DEFAULT_API = "https://dev.to"


class DevtoConnector(Connector):
    key = "devto"

    def _api(self, destination: Destination | None, connection: Connection | None) -> str:
        url = (destination.config.get("api_base_url") if destination else None) or \
              (connection.base_url if connection else None) or DEFAULT_API
        return url.rstrip("/")

    def _headers(self, connection: Connection) -> dict[str, str]:
        return {"api-key": self.credential(connection, "api_key"), "Accept": "application/vnd.forem.api-v1+json"}

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        api = self._api(destination, connection)
        article: dict[str, Any] = {
            "title": rendition.title,
            "body_markdown": rendition.body,
            "published": destination.config.get("publish_as", "published") == "published",
            "tags": rendition.tags[:4],
        }
        if rendition.canonical_url:
            article["canonical_url"] = rendition.canonical_url
        if destination.config.get("organization_id"):
            article["organization_id"] = destination.config["organization_id"]
        cover = next((m.url for m in rendition.media if m.type == "image"), None)
        if cover:
            article["main_image"] = cover
        resp = await self.http(api).request("POST", f"{api}/api/articles", json={"article": article},
                                            headers=self._headers(connection))
        data = resp.json()
        aid = require(data, "id")
        url = require(data, "url")
        return {"platform_post_id": encode_post_id(aid, api), "live_url": url,
                "status": "live" if data.get("published", True) else "draft"}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, api = decode_post_id(platform_post_id, connection)
        api = (api or DEFAULT_API).rstrip("/")
        try:
            resp = await self.http(api).request("GET", f"{api}/api/articles/{native}", headers=self._headers(connection))
        except ConnectorError as exc:
            if exc.details and exc.details.get("http_status") == 404:
                return {"status": "deleted", "live_url": None}  # unpublished or removed
            raise
        data = resp.json()
        return {"status": "live" if data.get("published", True) else "draft", "live_url": require(data, "url")}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, api = decode_post_id(platform_post_id, connection)
        api = (api or DEFAULT_API).rstrip("/")
        resp = await self.http(api).request("PUT", f"{api}/api/articles/{native}",
                                            json={"article": {"published": False}}, headers=self._headers(connection))
        return {"deleted": resp.json().get("published") is False, "method": "unpublished (Forem has no delete API)"}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        native, api = decode_post_id(platform_post_id, connection)
        api = (api or DEFAULT_API).rstrip("/")
        resp = await self.http(api).request("GET", f"{api}/api/articles/me/all", params={"per_page": 100},
                                            headers=self._headers(connection))
        row = next((a for a in resp.json() if str(a.get("id")) == str(native)), None)
        out = empty_metrics()
        if row:
            out.update(views=normalise_number(row.get("page_views_count")),
                       likes=normalise_number(row.get("public_reactions_count")),
                       comments=normalise_number(row.get("comments_count")))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        api = self._api(destination, connection)
        resp = await self.http(api).request("GET", f"{api}/api/articles/me/all", params={"per_page": 30},
                                            headers=self._headers(connection))
        for a in resp.json():
            if a.get("title", "").strip() == rendition.title.strip() and a.get("published", True):
                return {"platform_post_id": encode_post_id(a["id"], api), "live_url": a.get("url"), "status": "live"}
        return {}
