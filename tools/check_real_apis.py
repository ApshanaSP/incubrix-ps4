"""Runs the connectors' read-only methods against the REAL platforms (public posts, no account, nothing posted),
to show that the fields the code relies on exist in the real APIs, not only in our mock platforms.

    python -m tools.check_real_apis

For each platform: find a public post through the public API, then call the connector's own get_status,
fetch_metrics and certify_probe on it, with the sign-in header switched off (public reads need none).
robots.txt is checked before every URL. Writes outputs/metrics/real_api_check.json.
Not covered: publish and delete (they need an account; Lemmy and Ghost were covered on real local instances),
Ghost (its Admin API has no public reads) and Slack (every call needs a workspace token).
Hashnode: since 13 May 2026 every API call needs a Pro plan; the check confirms the connector reports that clearly.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

os.environ["SIMULATION_MODE"] = "false"  # real platforms; must be set before the app modules load

import httpx  # noqa: E402

from app.catalogue.intel import robots_allows  # noqa: E402
from app.config import get_settings, reload_settings  # noqa: E402
from app.connectors import registry  # noqa: E402
from app.connectors.base import Connection, Destination  # noqa: E402
from app.errors import ConnectorError  # noqa: E402

reload_settings()
ROOT = Path(__file__).resolve().parent.parent


async def public_json(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    if not await robots_allows(url, client):
        raise RuntimeError(f"robots.txt does not allow {url}")
    r = await client.request(kw.pop("method", "GET"), url, **kw)
    r.raise_for_status()
    return r.json()


async def check(name: str, steps) -> dict[str, Any]:
    t0 = time.time()
    try:
        detail = await steps()
        return {"platform": name, "ok": True, "seconds": round(time.time() - t0, 2), **detail}
    except Exception as exc:  # noqa: BLE001 - the report records any failure with its reason
        return {"platform": name, "ok": False, "seconds": round(time.time() - t0, 2), "error": f"{type(exc).__name__}: {exc}"}


async def main_async() -> int:
    ua = {"User-Agent": get_settings().user_agent}
    results = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=ua) as client:

        async def discourse():
            c = registry.get("discourse")
            c._headers = lambda conn: {"Accept": "application/json"}  # public reads need no Api-Key
            base = "https://meta.discourse.org"
            probe = await c.certify_probe(Destination(url=base, config={"base_url": base}), Connection())
            topic = (await public_json(client, f"{base}/latest.json"))["topic_list"]["topics"][0]
            first = (await public_json(client, f"{base}/t/{topic['id']}.json"))["post_stream"]["posts"][0]["id"]
            pid = f"{first}|{base}"
            return {"instance": base, "post": pid, "certify_probe": probe, "get_status": await c.get_status(pid, Connection()),
                    "fetch_metrics": await c.fetch_metrics(pid, Connection(), None)}

        async def devto():
            c = registry.get("devto")
            c._headers = lambda conn: {"Accept": "application/vnd.forem.api-v1+json"}
            art = (await public_json(client, "https://dev.to/api/articles", params={"per_page": 1}))[0]
            pid = f"{art['id']}|https://dev.to"
            return {"instance": "https://dev.to", "post": pid, "get_status": await c.get_status(pid, Connection()),
                    "note": "fetch_metrics reads the account's own articles (needs its key), not checked"}

        async def lemmy():
            c = registry.get("lemmy")
            base = "https://lemmy.world"

            async def no_auth(b, conn, method, url, **kw):  # public reads need no session
                return await c.http(b).request(method, url, **kw)

            c._authed = no_auth
            post = (await public_json(client, f"{base}/api/v3/post/list", params={"limit": 1, "sort": "Hot",
                                                                                  "type_": "Local"}))["posts"][0]["post"]
            pid = f"{post['id']}|{base}"
            community = await c._community_id(base, Destination(url=base, config={"base_url": base, "community": "technology"}))
            return {"instance": base, "post": pid, "community_id_lookup": community,
                    "get_status": await c.get_status(pid, Connection()), "fetch_metrics": await c.fetch_metrics(pid, Connection(), None)}

        async def hashnode():
            # since 13 May 2026 Hashnode's GraphQL API needs a Pro plan on the publication; the connector must say so
            c = registry.get("hashnode")
            try:
                await c.get_status("any-post|https://gql.hashnode.com", Connection(credentials={"token": "public-read"}))
            except ConnectorError as exc:
                if (exc.details or {}).get("cause") == "API_REQUIRES_PAID_PLAN":
                    return {"instance": "https://gql.hashnode.com", "finding": exc.message, "code": exc.code,
                            "handled": "the connector stops with this reason instead of crashing or retrying"}
                raise
            return {"instance": "https://gql.hashnode.com", "finding": "the API answered without a paid plan"}

        for name, fn in (("discourse", discourse), ("devto", devto), ("lemmy", lemmy), ("hashnode", hashnode)):
            res = await check(name, fn)
            results.append(res)
            print(("PASS " if res["ok"] else "FAIL ") + name + "  " + json.dumps({k: v for k, v in res.items()
                                                                                 if k not in ("platform", "ok")})[:300])
    out = ROOT / "outputs" / "metrics" / "real_api_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": results}, indent=2), encoding="utf-8")
    read = [r["platform"] for r in results if r["ok"] and "get_status" in r]
    changed = [r["platform"] for r in results if r["ok"] and "finding" in r]
    print(f"{len(read)} platforms answered with the fields our connectors use ({', '.join(read)}); "
          f"API changed and reported clearly: {', '.join(changed) or 'none'}; failed: "
          f"{', '.join(r['platform'] for r in results if not r['ok']) or 'none'}. {out}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
