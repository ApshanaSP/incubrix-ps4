"""Builds the real catalogue (Outstanding): catalogue/research_seed.csv -> catalogue/real_catalogue.csv.

For each researched destination:
  1. reachability: GET the URL, only where robots.txt allows (Reddit disallows everything, so its
     rows are never fetched)
  2. enrichment: member counts from public APIs (Discourse /about.json, Lemmy community API)
  3. rules: read the rules page (or the Lemmy API); the AI proposes posting rules with a quote for
     each. Where no rules text could be read, strict conservative defaults are used and the row is
     marked rules_source=conservative_default (a person must check it before any posting)
  4. relevance: the AI scores 1-5 against IncuBrix's audience with a one-line reason
Nothing is posted anywhere. Output rows use the importer's CSV format, so the file can be imported
with POST /api/v1/catalogue/imports/upload.

    USE_SYSTEM_CERTS=true AI_PROVIDER=groq GROQ_API_KEY=... python -m tools.build_real_catalogue
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.ai.providers import get_provider
from app.catalogue.intel import THEMES, fetch_page_text, heuristic_relevance, robots_allows
from app.config import get_settings
from app.errors import ApiError
from app.rules import parse_window

ROOT = Path(__file__).resolve().parent.parent / "catalogue"
SEED = ROOT / "research_seed.csv"
OUT_CSV = ROOT / "real_catalogue.csv"
OUT_EVIDENCE = ROOT / "real_catalogue_evidence.json"
CACHE = ROOT / ".ai_cache.json"

CONSERVATIVE = {"frequency_cap": "1/30d", "self_promo_allowed": "no", "link_policy": "one_link;no_shorteners",
                "disclosure_required": True, "automation_allowed": False}

SYSTEM = (
    "You assess one online community or publication for IncuBrix, a product that helps creators publish videos "
    "and posts everywhere with AI. IncuBrix's audience: AI, video, marketing, SaaS, creators and founders.\n"
    "1) From the RULES TEXT only (never guess), extract posting rules that matter to a brand sharing content: "
    "frequency_cap (like '1/7d', '2/day' or null), self_promo_allowed ('yes'|'no'|'limited'|null), link_policy "
    "(';'-joined from: any, none, one_link, no_shorteners, canonical_only; or null), disclosure_required "
    "(true|false|null: must AI-generated content be labelled), automation_allowed (true|false|null: are bots or "
    "automated posts allowed), and evidence: a list of {rule, quote} with the exact sentence from the text. If the "
    "rules text is empty, all rules are null and evidence is [].\n"
    "2) relevance: integer 1-5 (5 = core audience, 1 = unrelated) and relevance_reason: one sentence naming the "
    "audience overlap. audience_tags: subset of [ai, video, marketing, saas, creators, founders].\n"
    "Return one JSON object with keys: frequency_cap, self_promo_allowed, link_policy, disclosure_required, "
    "automation_allowed, evidence, relevance, relevance_reason, audience_tags."
)

FIELDS = ["name", "url", "platform_key", "connector_key", "surface", "category", "audience_tags", "language", "geo",
          "est_audience", "rules_url", "frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required",
          "automation_allowed", "relevance_score", "relevance_reason", "level", "community", "base_url",
          "rules_source", "rules_evidence", "reachable", "checked_at", "notes"]


async def lemmy_info(client: httpx.AsyncClient, url: str, community: str) -> dict[str, Any]:
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    out: dict[str, Any] = {"base_url": base}
    r = await client.get(f"{base}/api/v3/community", params={"name": community})
    if r.status_code != 200:
        out["exists"] = False
        return out
    view = r.json()["community_view"]
    c = view["community"]
    out.update(exists=True, members=view.get("counts", {}).get("subscribers"),
               posting_restricted=c.get("posting_restricted_to_mods"),
               text=f"Community '{c.get('title')}' description and rules:\n{c.get('description') or ''}")
    s = await client.get(f"{base}/api/v3/site")
    if s.status_code == 200:
        site = s.json().get("site_view", {})
        out["text"] += f"\n\nInstance sidebar:\n{(site.get('site') or {}).get('sidebar') or ''}"
        legal = (site.get("local_site") or {}).get("legal_information")
        if legal:
            out["text"] += f"\n\nInstance legal information:\n{legal}"
    out["text"] = out["text"][:7000]
    return out


async def discourse_members(client: httpx.AsyncClient, url: str) -> int | None:
    about = url.rstrip("/") + "/about.json"
    if not await robots_allows(about):
        return None
    try:
        r = await client.get(about)
        return int(r.json()["about"]["stats"].get("users_count") or r.json()["about"]["stats"].get("user_count"))
    except Exception:  # noqa: BLE001
        return None


async def gather_row(client: httpx.AsyncClient, row: dict[str, str]) -> dict[str, Any]:
    info: dict[str, Any] = {"row": row, "reachable": "", "members": None, "rules_text": "", "rules_from": ""}
    url = row["url"]
    if row["platform_key"] == "reddit":
        info["reachable"] = "not checked: reddit robots.txt disallows automated access"
        return info
    try:
        if row["platform_key"] == "lemmy":
            li = await lemmy_info(client, url, row["community"])
            info["reachable"] = "yes (community found via Lemmy API)" if li.get("exists") else "no: community not found"
            info["base_url"] = li["base_url"]
            if li.get("exists"):
                info["members"] = li.get("members")
                info["rules_text"] = li.get("text", "")
                info["rules_from"] = f"{li['base_url']}/api/v3/community?name={row['community']} (+ /api/v3/site)"
                if li.get("posting_restricted"):
                    info["rules_text"] += "\nOnly moderators may post in this community."
            return info
        if not await robots_allows(url):
            info["reachable"] = "not checked: robots.txt disallows"
        else:
            r = await client.get(url)
            if r.status_code < 400:
                info["reachable"] = "yes"
            elif r.status_code in (401, 403, 429, 503):
                # the site answers but blocks automated clients (bot protection); it exists
                info["reachable"] = f"blocked for bots: HTTP {r.status_code}"
            else:
                info["reachable"] = f"no: HTTP {r.status_code}"
        if row["platform_key"] == "discourse":
            info["members"] = await discourse_members(client, url)
        rules_url = row.get("rules_url")
        if rules_url:
            try:
                _, text = await fetch_page_text(rules_url, max_chars=7000)
                if len(text.strip()) > 200:
                    info["rules_text"], info["rules_from"] = text, rules_url
                else:
                    info["rules_note"] = "rules page has too little text (rendered by JavaScript)"
            except ApiError as exc:
                info["rules_note"] = exc.message
    except httpx.HTTPError as exc:
        info["reachable"] = info["reachable"] or f"no: {type(exc).__name__}"
    return info


async def assess(provider, info: dict[str, Any]) -> dict[str, Any]:
    row = info["row"]
    prompt = (f"NAME: {row['name']}\nURL: {row['url']}\nPLATFORM: {row['platform_key']}\nCATEGORY: {row['category']}\n"
              f"NOTES: {row.get('notes', '')}\n\nRULES TEXT:\n{info['rules_text'] or '(none could be read)'}")
    fallback = {**{k: None for k in CONSERVATIVE}, "evidence": [],
                **{"relevance": heuristic_relevance(f"{row['name']} {row['notes']} {row['audience_tags']}")["score"]}}
    for attempt in range(6):
        try:
            return await provider.json(SYSTEM, prompt, task="catalogue", fallback=fallback)
        except ApiError as exc:
            if attempt == 5:
                return {**fallback, "ai_error": exc.message}
            await asyncio.sleep(10 * (attempt + 1))
    return fallback


def clean(value: Any, kind: str) -> Any:
    if value in (None, "", "null"):
        return None
    if kind == "freq":
        try:
            parse_window(str(value))
            return str(value)
        except ValueError:
            return None
    if kind == "promo":
        v = str(value).lower()
        return v if v in ("yes", "no", "limited") else None
    if kind == "bool":
        return bool(value) if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1")
    if kind == "links":
        ok = {"any", "none", "one_link", "no_shorteners", "canonical_only"}
        toks = [t.strip() for t in str(value).split(";") if t.strip() in ok]
        return ";".join(toks) or None
    return value


async def main() -> int:
    settings = get_settings()
    provider = get_provider()
    seed = list(csv.DictReader(open(SEED, encoding="utf-8")))
    started = time.time()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": settings.user_agent}) as client:
        sem = asyncio.Semaphore(8)

        async def one(r):
            async with sem:
                return await gather_row(client, r)

        infos = await asyncio.gather(*(one(r) for r in seed))
    print(f"fetched {len(infos)} rows in {time.time() - started:.0f}s; asking {provider.name}/{provider.model}", flush=True)
    rows_out, evidence = [], {}
    ai_sem = asyncio.Semaphore(2)
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    async def judge(info):
        key = f"{info['row']['name']}|{len(info['rules_text'])}"
        if key in cache and not cache[key].get("ai_error"):
            return info, cache[key]
        async with ai_sem:
            res = await assess(provider, info)
        res["_model"] = f"{provider.name}/{provider.model}"
        cache[key] = res
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        return info, res

    results = await asyncio.gather(*(judge(i) for i in infos))
    for info, ai in results:
        r = info["row"]
        if str(info["reachable"]).startswith("no:"):
            print(f"  skip (unreachable): {r['name']} - {info['reachable']}")
            continue
        rules = {"frequency_cap": clean(ai.get("frequency_cap"), "freq"),
                 "self_promo_allowed": clean(ai.get("self_promo_allowed"), "promo"),
                 "link_policy": clean(ai.get("link_policy"), "links"),
                 "disclosure_required": clean(ai.get("disclosure_required"), "bool"),
                 "automation_allowed": clean(ai.get("automation_allowed"), "bool")}
        if info["rules_text"]:
            source = "ai_from_rules_page"
            # rules the page does not mention stay strict; a person confirms them
            filled = {k: (v if v is not None else CONSERVATIVE[k]) for k, v in rules.items()}
            defaulted = [k for k, v in rules.items() if v is None]
        else:
            source = "conservative_default"
            filled, defaulted = dict(CONSERVATIVE), list(CONSERVATIVE)
        if r["connector_key"] == "assisted":
            filled["automation_allowed"] = False
        tags = [t for t in (ai.get("audience_tags") or r["audience_tags"].split(";")) if t in THEMES]
        try:
            score = max(1, min(5, int(ai.get("relevance"))))
        except (TypeError, ValueError):
            score = heuristic_relevance(r["name"] + " " + r["notes"])["score"]
        level = {"assisted": "L1", "mrss": "L2", "webhook": "L2"}.get(r["connector_key"], "L0")
        rows_out.append({
            "name": r["name"], "url": r["url"], "platform_key": r["platform_key"], "connector_key": r["connector_key"],
            "surface": "", "category": r["category"], "audience_tags": ";".join(tags), "language": r["language"],
            "geo": "", "est_audience": info.get("members") or "", "rules_url": r["rules_url"],
            "frequency_cap": filled["frequency_cap"], "self_promo_allowed": filled["self_promo_allowed"],
            "link_policy": filled["link_policy"], "disclosure_required": str(filled["disclosure_required"]).lower(),
            "automation_allowed": str(filled["automation_allowed"]).lower(), "relevance_score": score,
            "relevance_reason": (ai.get("relevance_reason") or "")[:300], "level": level if r["connector_key"] == "assisted" else "L0",
            "community": r.get("community", ""), "base_url": info.get("base_url", ""),
            "rules_source": source + (f" (defaults for: {', '.join(defaulted)})" if defaulted and source != "conservative_default" else ""),
            "rules_evidence": len(ai.get("evidence") or []), "reachable": info["reachable"],
            "checked_at": time.strftime("%Y-%m-%d"), "notes": r["notes"],
        })
        evidence[r["name"]] = {"url": r["url"], "rules_read_from": info.get("rules_from") or None,
                               "rules_note": info.get("rules_note"), "ai_rules": rules,
                               "evidence": ai.get("evidence") or [], "ai_error": ai.get("ai_error"),
                               "model": ai.get("_model", "groq/openai/gpt-oss-120b")}
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows_out)
    OUT_EVIDENCE.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "provider": provider.usage.as_dict(), "destinations": evidence},
                                       indent=2, ensure_ascii=False), encoding="utf-8")
    by_source: dict[str, int] = {}
    for row in rows_out:
        k = row["rules_source"].split(" ")[0]
        by_source[k] = by_source.get(k, 0) + 1
    print(f"wrote {len(rows_out)} destinations to {OUT_CSV} ({by_source}); usage {provider.usage.as_dict()}; "
          f"{time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
