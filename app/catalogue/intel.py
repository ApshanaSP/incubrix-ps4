"""Catalogue intelligence: rules capture and relevance scoring.

Rules capture: fetch a community's rules page (robots.txt respected), have the
AI propose posting rules with a quote from the page for each, and store them
as a *proposal*. A person must confirm before they are used; until then the
destination's rules_status is 'proposed' and posting is blocked.

Relevance: score 1-5 against IncuBrix's audience (AI, video, marketing, SaaS,
creators, founders) with a one-line reason.
"""
from __future__ import annotations

import re
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from app.ai.providers import Provider, get_provider
from app.catalogue import store
from app.config import get_settings
from app.connectors.http import is_simulated
from app.db import get_db, new_id, now_iso
from app.errors import ApiError

# ---------------------------------------------------------------- fetching pages politely
async def fetch_page_text(url: str, max_chars: int = 12000) -> tuple[str, str]:
    """Return (title, visible text). Simulated hosts are served by the mock site."""
    s = get_settings()
    transport = None
    target = url
    if is_simulated(url) and not s.mock_base_url:
        from app.mocks.server import app as mock_app

        transport = httpx.ASGITransport(app=mock_app)
    if is_simulated(url):
        parts = urlsplit(url)
        target = f"{s.mock_base_url.rstrip('/') or 'http://mock.internal'}/sim/site/{parts.netloc}{parts.path or '/'}"
    elif not await robots_allows(url):
        raise ApiError("UPSTREAM_FETCH_FAILED", "robots.txt does not allow fetching this page", details={"url": url},
                       retryable=False)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=20, follow_redirects=True,
                                     headers={"User-Agent": s.user_agent}) as client:
            resp = await client.get(target)
    except httpx.HTTPError as exc:
        raise ApiError("UPSTREAM_FETCH_FAILED", f"could not fetch {url}: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise ApiError("UPSTREAM_FETCH_FAILED", f"{url} answered {resp.status_code}", details={"url": url})
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        return "", resp.text[:max_chars]
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n")).strip()
    return title, text[:max_chars]


_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


async def robots_allows(url: str, client: httpx.AsyncClient | None = None) -> bool:
    """Whether robots.txt lets our user agent fetch this URL. Pass the caller's client when checking many
    hosts: a new client per host builds a new TLS context each time, which is slow and blocks the event loop."""
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            if client is not None:
                resp = await client.get(root + "/robots.txt")
            else:
                async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                             headers={"User-Agent": get_settings().user_agent}) as own:
                    resp = await own.get(root + "/robots.txt")
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                _robots_cache[root] = rp
            else:
                _robots_cache[root] = None
        except httpx.HTTPError:
            _robots_cache[root] = None
    rp = _robots_cache[root]
    return True if rp is None else rp.can_fetch(get_settings().user_agent, url)


# ---------------------------------------------------------------- rules capture
RULES_SYSTEM = (
    "You read an online community's rules page and extract the posting rules that matter to a brand that wants "
    "to share content there. Answer only from the page; when the page says nothing about a rule, use null. "
    "Return JSON with keys: frequency_cap (string like '1/7d', '2/day' or null), self_promo_allowed ('yes'|'no'|"
    "'limited'|null), link_policy (';'-joined tokens from: any, none, one_link, no_shorteners, allowlist:<domains>, "
    "canonical_only; or null), disclosure_required (true|false|null: must AI-generated content be labelled), "
    "automation_allowed (true|false|null: are bots/automated posts allowed), language (ISO code or null), "
    "evidence (list of {rule, quote} with the exact sentence from the page), confidence (0-1)."
)


def heuristic_rules(text: str) -> dict[str, Any]:
    t = text.lower()
    out: dict[str, Any] = {"frequency_cap": None, "self_promo_allowed": None, "link_policy": None,
                           "disclosure_required": None, "automation_allowed": None, "language": None, "evidence": []}
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)

    def quote(*words: str) -> str:
        for s in sentences:
            if all(w in s.lower() for w in words):
                return s.strip()[:300]
        return ""

    if re.search(r"no (self[- ]?promotion|advertis|spam)|self[- ]?promotion is not allowed|do not advertise", t):
        out["self_promo_allowed"] = "no"
        out["evidence"].append({"rule": "self_promo_allowed", "quote": quote("promot") or quote("advertis")})
    elif re.search(r"self[- ]?promotion (is )?(allowed|ok|limited)|10% rule|9:1", t):
        out["self_promo_allowed"] = "limited"
        out["evidence"].append({"rule": "self_promo_allowed", "quote": quote("promot")})
    m = re.search(r"(one|1|two|2|three|3)\s+(link\s+)?posts?\s+(per|a|each)\s+(day|week|month)", t)
    if m:
        n = {"one": 1, "two": 2, "three": 3}.get(m.group(1), None) or int(m.group(1))
        unit = {"day": "1d", "week": "7d", "month": "30d"}[m.group(4)]
        out["frequency_cap"] = f"{n}/{unit}"
        out["evidence"].append({"rule": "frequency_cap", "quote": quote("per " + m.group(4)) or m.group(0)})
    tokens = []
    if "no links" in t or "links are not allowed" in t:
        tokens.append("none")
    if "shortener" in t:
        tokens.append("no_shorteners")
        out["evidence"].append({"rule": "link_policy", "quote": quote("shortener")})
    if tokens:
        out["link_policy"] = ";".join(tokens)
    if re.search(r"(ai|artificial intelligence|chatgpt|llm)[^.\n]{0,80}(label|disclos|tag|mark)", t) or \
            re.search(r"(label|disclos|tag|mark)[^.\n]{0,80}(ai|artificial intelligence)", t):
        out["disclosure_required"] = True
        out["evidence"].append({"rule": "disclosure_required", "quote": quote("ai") or quote("label")})
    if re.search(r"no bots|bots? (are )?not allowed|no automated|automated posting[^.\n]{0,40}(not|without)", t):
        out["automation_allowed"] = False
        out["evidence"].append({"rule": "automation_allowed", "quote": quote("bot") or quote("automat")})
    found = sum(1 for k in ("frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required",
                            "automation_allowed") if out[k] is not None)
    out["confidence"] = round(min(0.9, 0.3 + 0.12 * found), 2)
    return out


async def lemmy_rules_text(row: dict[str, Any]) -> tuple[str, str]:
    """Lemmy pages are rendered in the browser; the rules live in the API (community
    description, instance sidebar and legal text)."""
    cfg = row.get("config") or {}
    base = (cfg.get("base_url") or "").rstrip("/")
    parts: list[str] = []
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": get_settings().user_agent}) as client:
        if cfg.get("community"):
            r = await client.get(f"{base}/api/v3/community", params={"name": cfg["community"]})
            if r.status_code == 200:
                c = r.json()["community_view"]["community"]
                parts.append(f"Community {c.get('title')} rules and description:\n{c.get('description') or ''}")
        r = await client.get(f"{base}/api/v3/site")
        if r.status_code == 200:
            site = r.json().get("site_view", {}).get("site", {})
            parts.append(f"Instance sidebar:\n{site.get('sidebar') or ''}")
            legal = r.json().get("site_view", {}).get("local_site", {}).get("legal_information")
            if legal:
                parts.append(f"Instance legal information:\n{legal}")
    return f"{base} (Lemmy API)", "\n\n".join(parts)[:12000]


async def capture_rules(dest_id: str, provider: Provider | None = None, rules_url: str | None = None,
                        rules_text: str | None = None) -> dict[str, Any]:
    """rules_text: rules pasted by a person, for sites whose robots.txt or login wall stops fetching."""
    row = store.get_destination(dest_id)
    url = rules_url or row.get("rules_url")
    provider = provider or get_provider()
    if rules_text:
        title, text, url = "pasted by a person", rules_text[:12000], url or "pasted"
    elif row["connector_key"] == "lemmy" and not rules_url and not is_simulated((row.get("config") or {}).get("base_url", "")):
        title, text = await lemmy_rules_text(row)
        url = url or title
    elif not url:
        raise ApiError("VALIDATION_ERROR", "destination has no rules_url; pass rules_url or rules_text",
                       details={"field": "rules_url"})
    else:
        title, text = await fetch_page_text(url)
    fallback = heuristic_rules(text)
    try:
        proposed = await provider.json(RULES_SYSTEM, f"Community: {row['name']} ({row['url']})\nRules page {url}:\n\n{text}",
                                       task="rules_capture", fallback=fallback)
    except ApiError:
        proposed = {**fallback, "note": "AI provider failed; heuristic proposal shown"}
    proposed = _clean_rules(proposed)
    pid = new_id("rul")
    get_db().insert("rule_proposals", {"id": pid, "destination_id": dest_id, "source_url": url, "proposed": proposed,
                                       "evidence": proposed.get("evidence", []), "provider": provider.name,
                                       "status": "proposed", "created_at": now_iso()})
    store.update_destination(dest_id, {"rules_status": "proposed", "rules_url": url})
    return {"proposal_id": pid, "destination_id": dest_id, "source_url": url, "page_title": title,
            "proposed": proposed, "status": "proposed", "usage": provider.usage.as_dict(),
            "next": f"POST /api/v1/destinations/{dest_id}/rules/{pid}/confirm (a person reviews and confirms)"}


def _clean_rules(p: dict[str, Any]) -> dict[str, Any]:
    out = dict(p)
    sp = out.get("self_promo_allowed")
    if sp is not None and str(sp).lower() not in ("yes", "no", "limited") and not str(sp).lower().startswith("limited:"):
        out["self_promo_allowed"] = None
    fc = out.get("frequency_cap")
    if fc:
        from app.rules import parse_window

        try:
            parse_window(str(fc))
        except ValueError:
            out["frequency_cap"] = None
    return out


def confirm_rules(dest_id: str, proposal_id: str, decided_by: str, overrides: dict[str, Any] | None = None,
                  reject: bool = False) -> dict[str, Any]:
    prop = get_db().one("SELECT * FROM rule_proposals WHERE id=? AND destination_id=?", [proposal_id, dest_id])
    if not prop:
        raise ApiError("NOT_FOUND", f"rule proposal {proposal_id} not found")
    if reject:
        get_db().update("rule_proposals", "id", proposal_id, {"status": "rejected", "decided_by": decided_by,
                                                              "decided_at": now_iso()})
        store.update_destination(dest_id, {"rules_status": "confirmed"})
        return {"proposal_id": proposal_id, "status": "rejected"}
    rules = {k: v for k, v in (prop["proposed"] or {}).items()
             if k in ("frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed",
                      "language") and v is not None}
    rules.update({k: v for k, v in (overrides or {}).items() if k in rules or k in (
        "frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed", "language")})
    store.update_destination(dest_id, {**rules, "rules_status": "confirmed"})
    get_db().update("rule_proposals", "id", proposal_id, {"status": "confirmed", "decided_by": decided_by,
                                                          "decided_at": now_iso()})
    return {"proposal_id": proposal_id, "status": "confirmed", "applied": rules,
            "destination": store.public(store.get_destination(dest_id))}


# ---------------------------------------------------------------- relevance
THEMES: dict[str, list[str]] = {
    "ai": ["ai", "artificial intelligence", "machine learning", "llm", "gpt", "generative", "deep learning",
           "prompt", "chatbot", "ml", "neural", "openai", "stable diffusion", "midjourney"],
    "video": ["video", "youtube", "film", "editing", "streaming", "vlog", "animation", "motion", "premiere",
              "davinci", "videography", "shorts", "tiktok", "ctv"],
    "marketing": ["marketing", "seo", "growth", "ads", "social media", "content marketing", "brand", "copywriting",
                  "email marketing", "newsletter", "distribution", "audience"],
    "saas": ["saas", "b2b", "software", "devtools", "product", "api", "startup tools", "no-code", "nocode", "cloud"],
    "creators": ["creator", "creators", "influencer", "podcast", "writing", "artist", "youtuber", "content creator",
                 "creator economy", "patreon", "substack", "blogging"],
    "founders": ["founder", "founders", "startup", "startups", "entrepreneur", "indie hacker", "indiehackers",
                 "bootstrapp", "solopreneur", "vc", "fundraising", "side project", "launch"],
}
RELEVANCE_SYSTEM = (
    "You rate how relevant an online community or publication is for IncuBrix, a product that helps creators "
    "publish videos and posts everywhere with AI. IncuBrix's audience: AI, video, marketing, SaaS, creators and "
    "founders. Return JSON {score: 1-5 integer, reason: one sentence naming the audience overlap or the lack of it, "
    "audience_tags: list from [ai, video, marketing, saas, creators, founders]}. 5 = core audience, 1 = unrelated."
)


def heuristic_relevance(text: str) -> dict[str, Any]:
    t = f" {text.lower()} "
    hits: dict[str, list[str]] = {}
    for theme, words in THEMES.items():
        for w in words:
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t):
                hits.setdefault(theme, []).append(w)
    n = len(hits)
    strength = sum(min(3, len(v)) for v in hits.values())
    score = 1 if n == 0 else 2 if strength <= 1 else 3 if n == 1 else 4 if n == 2 else 5
    if n == 0:
        reason = "No overlap with IncuBrix's audience (AI, video, marketing, SaaS, creators, founders)."
    else:
        parts = [f"{k} ({', '.join(v[:2])})" for k, v in sorted(hits.items(), key=lambda kv: -len(kv[1]))]
        reason = f"Audience overlap: {'; '.join(parts)}."
    return {"score": score, "reason": reason, "audience_tags": sorted(hits)}


def describe(row: dict[str, Any], extra: str = "") -> str:
    return " | ".join(str(x) for x in (
        row.get("name"), row.get("url"), row.get("category"), " ".join(row.get("audience_tags") or []),
        (row.get("config") or {}).get("description", ""), extra) if x)


async def score_relevance(row: dict[str, Any], provider: Provider | None = None, extra: str = "") -> dict[str, Any]:
    provider = provider or get_provider()
    text = describe(row, extra)
    fallback = heuristic_relevance(text)
    try:
        res = await provider.json(RELEVANCE_SYSTEM, f"Community: {text}", task="relevance", fallback=fallback)
    except ApiError:
        res = {**fallback, "note": "AI provider failed; heuristic score"}
    try:
        score = max(1, min(5, int(res.get("score", fallback["score"]))))
    except (TypeError, ValueError):
        score = fallback["score"]
    tags = [t for t in (res.get("audience_tags") or fallback["audience_tags"]) if t in THEMES]
    return {"score": score, "reason": str(res.get("reason") or fallback["reason"])[:300], "audience_tags": tags}
