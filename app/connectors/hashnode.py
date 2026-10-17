"""Hashnode publications through the GraphQL API (gql.hashnode.com).
Any publication: config.publication_id. GraphQL errors arrive with HTTP 200,
so they are mapped from `errors[].extensions.code` through the manifest."""
from __future__ import annotations

from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id, normalise_number)
from app.connectors.http import require
from app.errors import ConnectorError

DEFAULT_API = "https://gql.hashnode.com"

PUBLISH = """mutation PublishPost($input: PublishPostInput!) {
  publishPost(input: $input) { post { id slug url title } }
}"""
REMOVE = """mutation RemovePost($input: RemovePostInput!) { removePost(input: $input) { post { id } } }"""
POST = """query Post($id: ID!) { post(id: $id) { id url views reactionCount responseCount } }"""
MY_POSTS = """query MyPosts { me { posts(pageSize: 20, page: 1) { nodes { id title url } } } }"""


class HashnodeConnector(Connector):
    key = "hashnode"

    def _api(self, destination: Destination | None, connection: Connection | None) -> str:
        url = (destination.config.get("api_base_url") if destination else None) or \
              (connection.base_url if connection else None) or DEFAULT_API
        return url.rstrip("/")

    async def _gql(self, api: str, connection: Connection, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.http(api).request("POST", f"{api}/", json={"query": query, "variables": variables},
                                                headers={"Authorization": self.credential(connection, "token")})
        except ConnectorError as exc:
            if (exc.details or {}).get("cause") == "API_MOVED":
                # checked on 17 Oct 2026 (tools/check_real_apis.py): gql.hashnode.com redirects every call
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE",
                    "Hashnode's GraphQL API needs a Pro plan on the publication since 13 May 2026 "
                    "(https://hashnode.com/changelog/2026-05-13-graphql-api-paid-access); upgrade the publication, "
                    "or post there through the assisted pack", retryable=False,
                    details={**(exc.details or {}), "cause": "API_REQUIRES_PAID_PLAN"}) from None
            raise
        data = resp.json()
        if data.get("errors"):
            err = data["errors"][0]
            gql_code = (err.get("extensions") or {}).get("code", "")
            code = self.manifest.get("error_map", {}).get(gql_code, "PLATFORM_CONTENT_REJECTED")
            raise ConnectorError(code, f"Hashnode: {err.get('message')}", details={"graphql_code": gql_code,
                                                                                   "platform_message": err.get("message")})
        return require(data, "data")

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        api = self._api(destination, connection)
        pub = destination.config.get("publication_id")
        if not pub:
            raise ConnectorError("PLATFORM_UNAVAILABLE", "destination has no publication_id", retryable=False,
                                 details={"cause": "MISSING_INSTANCE_PARAM", "param": "publication_id"})
        inp: dict[str, Any] = {"title": rendition.title, "contentMarkdown": rendition.body, "publicationId": pub,
                               "tags": [{"slug": t.lower(), "name": t} for t in rendition.tags[:5]]}
        if rendition.canonical_url:
            inp["originalArticleURL"] = rendition.canonical_url
        data = await self._gql(api, connection, PUBLISH, {"input": inp})
        post = require(data, "publishPost", "post")
        return {"platform_post_id": encode_post_id(require(post, "id"), api), "live_url": require(post, "url"),
                "status": "live"}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, api = decode_post_id(platform_post_id, connection)
        data = await self._gql((api or DEFAULT_API), connection, POST, {"id": native})
        post = data.get("post")
        if not post:
            return {"status": "deleted", "live_url": None}
        return {"status": "live", "live_url": require(post, "url")}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        native, api = decode_post_id(platform_post_id, connection)
        data = await self._gql((api or DEFAULT_API), connection, REMOVE, {"input": {"id": native}})
        return {"deleted": bool(require(data, "removePost", "post", "id"))}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        native, api = decode_post_id(platform_post_id, connection)
        data = await self._gql((api or DEFAULT_API), connection, POST, {"id": native})
        post = data.get("post") or {}
        out = empty_metrics()
        out.update(views=normalise_number(post.get("views")), likes=normalise_number(post.get("reactionCount")),
                   comments=normalise_number(post.get("responseCount")))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        api = self._api(destination, connection)
        data = await self._gql(api, connection, MY_POSTS, {})
        for p in require(data, "me", "posts", "nodes"):
            if p.get("title", "").strip() == rendition.title.strip():
                return {"platform_post_id": encode_post_id(p["id"], api), "live_url": p.get("url"), "status": "live"}
        return {}
