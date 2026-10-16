"""MRSS video feed for CTV apps and video aggregators (feed tier).

Publishing adds an item to a Media RSS feed that IncuBrix hosts at
/api/v1/feeds/{feed_key}.xml; aggregators poll that URL. The feed file lives
in object storage (feeds/{feed_key}.json + .xml), which plays the role of the
"platform" for this connector.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any
from xml.sax.saxutils import escape

from app.config import get_settings
from app.connectors.base import Connection, Connector, Destination, Rendition, decode_post_id
from app.errors import ConnectorError
from app.storage import get_storage


def _load(feed_key: str) -> dict[str, Any]:
    try:
        return json.loads(get_storage().get_bytes(f"feeds/{feed_key}.json"))
    except Exception:
        return {"feed_key": feed_key, "items": []}


def render_feed(feed: dict[str, Any], title: str | None = None) -> str:
    base = get_settings().public_base_url.rstrip("/")
    key = feed["feed_key"]
    items = []
    for it in feed["items"]:
        media = it.get("media") or {}
        media_xml = ""
        if media.get("url"):
            attrs = f'url="{escape(media["url"])}" medium="{escape(media.get("type", "video"))}"'
            if media.get("mime"):
                attrs += f' type="{escape(media["mime"])}"'
            if media.get("duration_s"):
                attrs += f' duration="{int(media["duration_s"])}"'
            media_xml = f"<media:content {attrs}><media:title>{escape(it['title'])}</media:title></media:content>"
        if it.get("thumbnail"):
            media_xml += f'<media:thumbnail url="{escape(it["thumbnail"])}"/>'
        items.append(
            "<item>"
            f"<title>{escape(it['title'])}</title>"
            f"<link>{escape(it.get('link') or base)}</link>"
            f'<guid isPermaLink="false">{escape(it["guid"])}</guid>'
            f"<pubDate>{it['pub_date']}</pubDate>"
            f"<description>{escape(it.get('description', ''))}</description>"
            f"{media_xml}"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        f"<title>{escape(title or feed.get('title') or 'IncuBrix ' + key)}</title>"
        f"<link>{escape(base)}</link>"
        f'<atom:link href="{escape(base)}/api/v1/feeds/{escape(key)}.xml" rel="self" type="application/rss+xml"/>'
        "<description>IncuBrix creator video feed</description>"
        + "".join(items)
        + "</channel></rss>"
    )


def _save(feed: dict[str, Any]) -> None:
    storage = get_storage()
    storage.put_json(f"feeds/{feed['feed_key']}.json", feed)
    storage.put_text(f"feeds/{feed['feed_key']}.xml", render_feed(feed), "application/rss+xml")


class MrssConnector(Connector):
    key = "mrss"

    def _feed_key(self, destination: Destination) -> str:
        key = destination.config.get("feed_key")
        if not key:
            raise ConnectorError("PLATFORM_UNAVAILABLE", "destination has no feed_key", retryable=False,
                                 details={"cause": "MISSING_INSTANCE_PARAM", "param": "feed_key"})
        return str(key)

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        key = self._feed_key(destination)
        feed = _load(key)
        existing = next((i for i in feed["items"] if i.get("idempotency_key") == idempotency_key), None)
        guid = existing["guid"] if existing else f"ibx-{uuid.uuid4().hex[:12]}"
        if not existing:
            video = next((m for m in rendition.media if m.type == "video"), None)
            thumb = next((m.url for m in rendition.media if m.type == "image"), None)
            feed["items"].insert(0, {
                "guid": guid, "idempotency_key": idempotency_key, "title": rendition.title,
                "description": rendition.body[:5000], "link": rendition.link or rendition.canonical_url,
                "media": video.model_dump(exclude_none=True) if video else None, "thumbnail": thumb,
                "pub_date": format_datetime(datetime.now(timezone.utc)),
            })
            feed["items"] = feed["items"][: int(destination.config.get("max_items", 100))]
            _save(feed)
        live = f"{get_settings().public_base_url.rstrip('/')}/api/v1/feeds/{key}.xml#{guid}"
        return {"platform_post_id": f"{guid}|{key}", "live_url": live, "status": "live"}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        guid, key = decode_post_id(platform_post_id)
        feed = _load(key or "")
        if any(i["guid"] == guid for i in feed["items"]):
            return {"status": "live",
                    "live_url": f"{get_settings().public_base_url.rstrip('/')}/api/v1/feeds/{key}.xml#{guid}"}
        return {"status": "deleted", "live_url": None}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        guid, key = decode_post_id(platform_post_id)
        feed = _load(key or "")
        before = len(feed["items"])
        feed["items"] = [i for i in feed["items"] if i["guid"] != guid]
        _save(feed)
        return {"deleted": True, "already_gone": before == len(feed["items"])}

    async def find_existing(self, rendition: Rendition, destination: Destination, connection: Connection,
                            idempotency_key: str) -> dict[str, Any] | None:
        key = self._feed_key(destination)
        item = next((i for i in _load(key)["items"] if i.get("idempotency_key") == idempotency_key), None)
        if not item:
            return {}
        return {"platform_post_id": f"{item['guid']}|{key}",
                "live_url": f"{get_settings().public_base_url.rstrip('/')}/api/v1/feeds/{key}.xml#{item['guid']}",
                "status": "live"}
