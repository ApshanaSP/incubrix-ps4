"""Slack app (bot token) posting into channels the app was invited to.
Any workspace/channel: config.channel. Slack reports most errors as HTTP 200
with {"ok": false, "error": "..."}; those are mapped through the manifest.
The idempotency key travels as message metadata, so a retry after a timeout
can find the message it already sent."""
from __future__ import annotations

from typing import Any

from app.connectors.base import (Connection, Connector, Destination, Rendition, decode_post_id, empty_metrics,
                                 encode_post_id)
from app.connectors.http import require
from app.errors import ConnectorError

DEFAULT_API = "https://slack.com"


class SlackConnector(Connector):
    key = "slack"

    def _api(self, destination: Destination | None, connection: Connection | None) -> str:
        url = (destination.config.get("api_base_url") if destination else None) or DEFAULT_API
        return url.rstrip("/")

    async def _call(self, api: str, connection: Connection, method: str, args: dict[str, Any], http_method: str = "POST") -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.credential(connection, 'bot_token')}"}
        if http_method == "GET":
            resp = await self.http(api).request("GET", f"{api}/api/{method}", params=args, headers=headers)
        else:
            resp = await self.http(api).request("POST", f"{api}/api/{method}", json=args,
                                                headers={**headers, "Content-Type": "application/json; charset=utf-8"})
        data = resp.json()
        if not data.get("ok", False):
            err = data.get("error", "unknown_error")
            code = self.manifest.get("error_map", {}).get(err)
            if code is None:
                code = "PLATFORM_CONTENT_REJECTED" if err.endswith(("_too_long", "_invalid")) else "PLATFORM_UNAVAILABLE"
            raise ConnectorError(code, f"Slack {method}: {err}", details={"slack_error": err, "platform_message": err})
        return data

    @staticmethod
    def _text(rendition: Rendition) -> str:
        parts = [f"*{rendition.title}*"] if rendition.title else []
        if rendition.body:
            parts.append(rendition.body)
        if rendition.link and rendition.link not in rendition.body:
            parts.append(rendition.link)
        return "\n\n".join(parts)

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        self.check_auth_status(connection)
        api = self._api(destination, connection)
        channel = destination.config.get("channel")
        if not channel:
            raise ConnectorError("PLATFORM_UNAVAILABLE", "destination has no channel", retryable=False,
                                 details={"cause": "MISSING_INSTANCE_PARAM", "param": "channel"})
        data = await self._call(api, connection, "chat.postMessage", {
            "channel": channel,
            "text": self._text(rendition),
            "unfurl_links": True,
            "metadata": {"event_type": "incubrix_post", "event_payload": {"idempotency_key": idempotency_key}},
        })
        ts = require(data, "ts")
        ch = data.get("channel", channel)
        link = await self._call(api, connection, "chat.getPermalink", {"channel": ch, "message_ts": ts}, "GET")
        return {"platform_post_id": encode_post_id(f"{ch}/{ts}", api), "live_url": require(link, "permalink"),
                "status": "live"}

    def _split(self, platform_post_id: str, connection: Connection) -> tuple[str, str, str]:
        native, api = decode_post_id(platform_post_id, connection)
        channel, ts = native.split("/", 1)
        return (api or DEFAULT_API).rstrip("/"), channel, ts

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        api, channel, ts = self._split(platform_post_id, connection)
        try:
            link = await self._call(api, connection, "chat.getPermalink", {"channel": channel, "message_ts": ts}, "GET")
        except ConnectorError as exc:
            if (exc.details or {}).get("slack_error") == "message_not_found":
                return {"status": "deleted", "live_url": None}
            raise
        return {"status": "live", "live_url": require(link, "permalink")}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        api, channel, ts = self._split(platform_post_id, connection)
        try:
            await self._call(api, connection, "chat.delete", {"channel": channel, "ts": ts})
        except ConnectorError as exc:
            if (exc.details or {}).get("slack_error") == "message_not_found":
                return {"deleted": True, "already_gone": True}
            raise
        return {"deleted": True}

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        api, channel, ts = self._split(platform_post_id, connection)
        data = await self._call(api, connection, "reactions.get", {"channel": channel, "timestamp": ts, "full": True}, "GET")
        msg = data.get("message", {})
        out = empty_metrics()
        out["likes"] = sum(int(r.get("count", 0)) for r in msg.get("reactions", []))
        out["comments"] = int(msg.get("reply_count", 0))
        return out

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        api = self._api(destination, connection)
        channel = destination.config.get("channel")
        data = await self._call(api, connection, "conversations.history",
                                {"channel": channel, "limit": 50, "include_all_metadata": True}, "GET")
        for m in data.get("messages", []):
            payload = ((m.get("metadata") or {}).get("event_payload") or {})
            if payload.get("idempotency_key") == idempotency_key:
                pid = encode_post_id(f"{channel}/{m['ts']}", api)
                link = await self._call(api, connection, "chat.getPermalink", {"channel": channel, "message_ts": m["ts"]}, "GET")
                return {"platform_post_id": pid, "live_url": link.get("permalink"), "status": "live"}
        return {}

    async def certify_probe(self, destination: Destination, connection: Connection) -> dict[str, Any]:
        data = await self._call(self._api(destination, connection), connection, "auth.test", {})
        return {"ok": True, "team": data.get("team")}
