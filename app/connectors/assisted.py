"""Assisted tier: where automation is not allowed, build a ready-to-post pack
and let a person post it.

The pack holds the text, the media, the link, a rules checklist built from the
destination's rules and a submit link (prefilled where the site supports it).
Pack state lives in object storage next to the pack (packs/{id}/pack.json), not
in the service database, because connector methods only return results.
"""
from __future__ import annotations

import io
import json
import mimetypes
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.config import get_settings
from app.connectors.base import Connection, Connector, Destination, Rendition, decode_post_id
from app.errors import ConnectorError
from app.storage import get_storage

MAX_MEDIA_BYTES = 25 * 1024 * 1024


def rules_checklist(rendition: Rendition, destination: Destination, spec: dict[str, Any]) -> list[str]:
    items = [f"Post from the official IncuBrix account on {destination.name or destination.url}."]
    if destination.rules_url:
        items.append(f"Re-read the community rules before posting: {destination.rules_url}")
    if destination.frequency_cap:
        items.append(f"Frequency cap is {destination.frequency_cap}: check that IncuBrix has not posted here inside that window.")
    promo = (destination.self_promo_allowed or "yes").lower()
    if promo in ("no", "false", "never"):
        items.append("Self-promotion is NOT allowed: keep it discussion-first, no calls to action.")
    elif promo.startswith("limited"):
        items.append("Self-promotion is limited: at most one promotional post in the window; lead with value.")
    policy = (destination.link_policy or "any").lower()
    if policy != "any":
        items.append(f"Link policy: {destination.link_policy}.")
    if destination.disclosure_required and rendition.ai_generated:
        items.append("Keep the AI-content disclosure line in the post.")
    if spec.get("title_max_chars"):
        items.append(f"Title must stay under {spec['title_max_chars']} characters (it is {len(rendition.title)}).")
    if destination.language and rendition.language and destination.language[:2] != rendition.language[:2]:
        items.append(f"The community writes in '{destination.language}': post a translated version.")
    items.append("After posting, paste the live URL back into IncuBrix (POST /api/v1/assist/{pack_id}/posted).")
    items.append("Answer comments within 24 hours; do not cross-post the same text to sibling communities.")
    return items


def deep_link(rendition: Rendition, destination: Destination) -> str | None:
    template = destination.config.get("submit_url_template")
    if template:
        return template.format(title=quote(rendition.title), url=quote(rendition.link or rendition.canonical_url or ""),
                               body=quote(rendition.body[:1500]))
    return destination.config.get("submit_url") or destination.url or None


class AssistedConnector(Connector):
    key = "assisted"

    async def build_assist_pack(self, rendition: Rendition, destination: Destination) -> dict[str, Any]:
        storage = get_storage()
        pack_id = f"pack_{uuid.uuid4().hex[:12]}"
        prefix = f"packs/{pack_id}"
        spec = self.surface_spec(destination, rendition.surface)
        checklist = rules_checklist(rendition, destination, spec)
        caption = rendition.title + ("\n\n" + rendition.body if rendition.body else "")
        link = deep_link(rendition, destination)
        files: list[dict[str, Any]] = []
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            post_md = f"# {rendition.title}\n\n{rendition.body}\n"
            if rendition.link:
                post_md += f"\nLink: {rendition.link}\n"
            for name, text in (("post.md", post_md), ("caption.txt", caption),
                               ("checklist.md", "\n".join(f"- [ ] {c}" for c in checklist) + "\n")):
                files.append({"name": name, "url": storage.put_text(f"{prefix}/{name}", text)})
                z.writestr(name, text)
            for i, m in enumerate(rendition.media):
                entry: dict[str, Any] = {"name": f"media_{i + 1}", "source_url": m.url, "type": m.type}
                data = await _fetch_media(m.url)
                if data:
                    ext = mimetypes.guess_extension(m.mime or "") or (urlsplit(m.url).path.rsplit(".", 1)[-1:] or ["bin"])[0]
                    ext = ext if ext.startswith(".") else f".{ext}"
                    name = f"media_{i + 1}{ext}"
                    entry.update(name=name, url=storage.put_bytes(f"{prefix}/{name}", data, m.mime), bytes=len(data))
                    z.writestr(name, data)
                else:
                    entry["note"] = "media could not be downloaded; use the source URL"
                files.append(entry)
        files.append({"name": "pack.zip", "url": storage.put_bytes(f"{prefix}/pack.zip", zbuf.getvalue(), "application/zip")})
        state = {"pack_id": pack_id, "destination": destination.name, "status": "awaiting_human", "live_url": None,
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "deep_link": link}
        storage.put_json(f"{prefix}/pack.json", state)
        return {"files": files, "caption": caption, "deep_link": link, "checklist": checklist, "pack_id": pack_id}

    async def publish(self, rendition: Rendition, destination: Destination, connection: Connection,
                      idempotency_key: str) -> dict[str, Any]:
        pack = await self.build_assist_pack(rendition, destination)
        return {"platform_post_id": f"assist:{pack['pack_id']}", "live_url": None, "status": "awaiting_human",
                "assist_pack": pack}

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        state = _load_state(platform_post_id)
        return {"status": state["status"], "live_url": state.get("live_url")}

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        state = _load_state(platform_post_id)
        if state["status"] == "posted":
            # a person posted it by hand, so a person must remove it
            state["status"] = "removal_requested"
            _save_state(state)
            return {"deleted": False, "action_required": "A person posted this; ask them to remove it at "
                                                         f"{state.get('live_url')}"}
        state["status"] = "discarded"
        _save_state(state)
        return {"deleted": True}


def _native(platform_post_id: str) -> str:
    native, _ = decode_post_id(platform_post_id)
    if not native.startswith("assist:"):
        raise ConnectorError("PLATFORM_CONTENT_REJECTED", "not an assisted pack id", details={"id": platform_post_id})
    return native.split(":", 1)[1]


def _load_state(platform_post_id: str) -> dict[str, Any]:
    pack_id = _native(platform_post_id)
    try:
        return json.loads(get_storage().get_bytes(f"packs/{pack_id}/pack.json"))
    except Exception:
        raise ConnectorError("PLATFORM_CONTENT_REJECTED", f"assist pack {pack_id} not found",
                             details={"http_status": 404}) from None


def _save_state(state: dict[str, Any]) -> None:
    get_storage().put_json(f"packs/{state['pack_id']}/pack.json", state)


def mark_posted(pack_id: str, live_url: str) -> dict[str, Any]:
    state = _load_state(f"assist:{pack_id}")
    state.update(status="posted", live_url=live_url,
                 posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _save_state(state)
    return state


async def _fetch_media(url: str) -> bytes | None:
    if not url.startswith(("http://", "https://")):
        return None
    if get_settings().simulation_mode and (urlsplit(url).hostname or "").endswith((".mock", "example.com", ".test")):
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_MEDIA_BYTES:
                        return None
                return bytes(buf)
    except httpx.HTTPError:
        return None
