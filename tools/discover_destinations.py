"""Finds more real, relevant destinations whose posting rules are public and machine-readable, and adds them to
catalogue/research_seed.csv for tools/build_real_catalogue.py (which reads and verifies the rules).

    python -m tools.discover_destinations [--dry-run]

Sources, read only through public APIs that robots.txt allows (nothing is posted, no account is used):
  Lemmy communities   GET /api/v3/community/list on well-known instances; a community's description holds its rules
  PeerTube instances  GET https://instances.joinpeertube.org/api/v1/instances, then each instance's
                      /api/v1/config/about (terms, code of conduct, moderation information)
Kept only when the name or description matches IncuBrix's audience (AI, video, creators, marketing, SaaS,
founders) and the description reads like rules. Discourse forums are not added: their default robots.txt rule
"Disallow: /g" also covers /guidelines, so their rules cannot be read automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.catalogue.intel import robots_allows
from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "catalogue" / "research_seed.csv"

LEMMY_INSTANCES = ["lemmy.world", "lemmy.ml", "programming.dev", "sh.itjust.works", "lemmy.zip", "lemmy.ca",
                   "lemmy.dbzer0.com", "discuss.tchncs.de", "lemmy.today", "slrpnk.net", "feddit.org", "beehaw.org",
                   "lemmy.blahaj.zone", "midwest.social", "lemmy.sdf.org", "infosec.pub"]
THEMES = {
    "ai": r"\b(ai|a\.i\.|artificial intelligence|machine learning|ml|llm|llms|chatgpt|gpt|stable ?diffusion|generative|"
          r"neural|deep learning|localllama|openai|midjourney)\b",
    "video": r"\b(video|videos|youtube|film|films|filmmaking|filmmakers|cinema|cinematography|animation|vfx|editing|"
             r"videography|streaming|peertube|documentar\w*|short films?)\b",
    "creators": r"\b(creators?|content creat\w+|podcasts?|podcasting|photography|youtubers?|streamers?|artists?|"
                r"illustration|design|music production)\b",
    "marketing": r"\b(marketing|seo|advertising|social media|copywriting|growth|newsletters?|branding)\b",
    "saas": r"\b(saas|startups?|software|selfhosted|self-hosted|open ?source|foss|webdev|web development|programming|"
            r"developers?|devops|technology|tech)\b",
    "founders": r"\b(founders?|entrepreneurs?|entrepreneurship|small business|business|indie hackers?|side projects?)\b",
}
CRUDE = re.compile(r"(fuck|shit|nsfw|porn|gore)", re.I)
RULE_WORDS = re.compile(r"\b(rules?|no (spam|self[- ]promo\w*|ads|advertis\w+|bots?)|self[- ]promo\w*|be (civil|respectful|"
                        r"nice|kind)|do not|don't|please (do not|don't)|prohibited|not allowed|allowed)\b", re.I)


def rank(tags: list[str], size: int) -> tuple[int, int]:
    """Audience fit first (the core themes count double; general tech alone counts least), then size."""
    core = sum(1 for t in tags if t != "saas")
    return (2 * core + (1 if "saas" in tags else 0), size)


def themes_of(text: str) -> list[str]:
    t = (text or "").lower()
    return [k for k, rx in THEMES.items() if re.search(rx, t)]


async def get_json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    if not await robots_allows(url, client):
        return None
    try:
        r = await client.get(url, params=params or None)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def lemmy(client: httpx.AsyncClient) -> list[dict[str, str]]:
    rows = []
    for inst in LEMMY_INSTANCES:
        for page in (1, 2, 3):
            data = await get_json(client, f"https://{inst}/api/v3/community/list", type_="Local", sort="TopAll",
                                  limit=50, page=page)
            items = (data or {}).get("communities") or []
            if not items:
                break
            for cv in items:
                c = cv.get("community") or {}
                desc = c.get("description") or ""
                if c.get("nsfw") or c.get("removed") or c.get("deleted") or c.get("posting_restricted_to_mods"):
                    continue
                # relevance from the name and title only: a rules text that says "no AI images" or "no
                # advertising" says nothing about what the community is about
                tags = themes_of(f"{c.get('name', '').replace('_', ' ')} {c.get('title')}")
                if not tags or len(desc) < 150 or not RULE_WORDS.search(desc):
                    continue
                lang = "de" if inst == "feddit.org" else "en"
                subs = int((cv.get("counts") or {}).get("subscribers") or 0)
                rows.append({"_rank": rank(tags, subs), "name": f"{inst} c/{c['name']}", "url": f"https://{inst}/c/{c['name']}", "platform_key": "lemmy",
                             "connector_key": "lemmy", "category": "community", "audience_tags": ";".join(tags),
                             "language": lang, "rules_url": "", "community": c["name"],
                             "notes": re.sub(r"\s+", " ", (c.get("title") or c["name"]))[:120]
                             + f" ({subs} subscribers)"})
    return rows


async def peertube(client: httpx.AsyncClient) -> list[dict[str, str]]:
    data = await get_json(client, "https://instances.joinpeertube.org/api/v1/instances", start=0, count=150,
                          sort="-totalUsers", healthy="true")
    rows = []
    for inst in (data or {}).get("data") or []:
        host = inst.get("host")
        if not host or inst.get("nsfwPolicy") == "display" or "en" not in (inst.get("languages") or ["en"]):
            continue
        about = await get_json(client, f"https://{host}/api/v1/config/about")
        a = (about or {}).get("instance") or {}
        rules = " ".join(str(a.get(k) or "") for k in ("terms", "codeOfConduct", "moderationInformation"))
        tags = themes_of(f"{a.get('name')} {a.get('shortDescription')} video")
        if len(rules) < 150 or not RULE_WORDS.search(rules):
            continue
        rows.append({"_rank": rank(tags, int(inst.get("totalUsers") or 0)), "name": f"{a.get('name') or host} (PeerTube)", "url": f"https://{host}", "platform_key": "peertube",
                     "connector_key": "assisted", "category": "video_platform", "audience_tags": ";".join(tags),
                     "language": "en", "rules_url": f"https://{host}/api/v1/config/about", "community": "",
                     "notes": re.sub(r"\s+", " ", a.get("shortDescription") or "PeerTube video instance")[:120]
                     + "; a person uploads"})
    return rows


async def main_async(dry: bool, max_lemmy: int, max_peertube: int) -> int:
    existing = list(csv.DictReader(open(SEED, encoding="utf-8")))
    fields = list(existing[0].keys())
    have = {urlsplit(r["url"].lower()).netloc + urlsplit(r["url"].lower()).path.rstrip("/") for r in existing}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": get_settings().user_agent}) as c:
        found = await lemmy(c) + await peertube(c)
    fresh, seen = [], set()
    found.sort(key=lambda r: r["_rank"], reverse=True)
    for r in found:
        k = urlsplit(r["url"].lower()).netloc + urlsplit(r["url"].lower()).path.rstrip("/")
        if k in have or k in seen:
            continue
        seen.add(k)
        if r["_rank"][0] < 2:  # general tech with no AI, video, creator, marketing or founder angle: not relevant enough
            continue
        if r["platform_key"] == "peertube" and r["_rank"][0] < 3:  # a generic video host: video alone is not enough
            continue
        if CRUDE.search(r["name"]):
            continue
        cap = max_lemmy if r["platform_key"] == "lemmy" else max_peertube
        if sum(1 for x in fresh if x["platform_key"] == r["platform_key"]) >= cap:
            continue
        fresh.append(r)
    by = {}
    for r in fresh:
        by[r["platform_key"]] = by.get(r["platform_key"], 0) + 1
    print(f"found {len(found)}, kept {len(fresh)} {by}")
    for r in fresh[:12]:
        print("  ", r["_rank"], r["name"], "|", r["audience_tags"])
    if not dry and fresh:
        with open(SEED, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore").writerows(fresh)
        print(f"appended {len(fresh)} rows to {SEED}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-lemmy", type=int, default=120)
    ap.add_argument("--max-peertube", type=int, default=30)
    a = ap.parse_args()
    return asyncio.run(main_async(a.dry_run, a.max_lemmy, a.max_peertube))


if __name__ == "__main__":
    raise SystemExit(main())
