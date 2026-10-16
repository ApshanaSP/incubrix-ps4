"""Posting-rules engine (must-have 4).

Checked before every post: frequency cap, self-promotion policy, link policy,
AI-content disclosure, plus automation permission and unconfirmed rules.
A violation is either `block` (with a reason) or `reschedule` (with the time
the cap window frees up). Nothing is posted while any violation stands.

Rule formats stored on a destination row:
  frequency_cap       "1/7d", "2/day", "1/week", "3/30d"; empty = no cap
  self_promo_allowed  "yes" | "no" | "limited" (limited = at most 1 promotional post per 30 days)
                      or "limited:N/window" (e.g. "limited:1/14d")
  link_policy         tokens joined by ";": any | none | one_link | no_shorteners |
                      allowlist:a.com,b.org | canonical_only
  disclosure_required true/false: AI-generated content must carry a disclosure line
  automation_allowed  false = native posting is not allowed; use the assisted pack
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from app.connectors.base import URL_RE, Destination, Rendition
from app.db import get_db

WINDOW_UNITS = {"h": 3600, "hour": 3600, "d": 86400, "day": 86400, "w": 604800, "week": 604800,
                "m": 2592000, "month": 2592000}

SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly", "is.gd", "rebrand.ly", "cutt.ly",
              "shorturl.at", "lnkd.in", "rb.gy"}

PROMO_PHRASES = [
    "sign up", "signup", "free trial", "try it free", "try it now", "buy now", "discount", "promo code", "coupon",
    "% off", "limited time", "subscribe now", "book a demo", "get started today", "check out our", "our product",
    "our platform", "our new tool", "use code", "early access", "join our waitlist", "pricing", "upgrade now",
]

DISCLOSURE_RE = re.compile(
    r"(ai[- ]generated|generated (with|by|using) ai|written (with|by) ai|ai[- ]assisted|created with the help of ai|"
    r"made with ai|#aigenerated|disclosure:)", re.I,
)


def own_domains() -> set[str]:
    raw = os.getenv("OWN_DOMAINS", "incubrix.com,incubrix.ai")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def parse_window(text: str) -> tuple[int, int] | None:
    """'2/7d' -> (2, 604800). Returns None when there is no cap."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(?:/|per)\s*(\d*)\s*([a-z]+)\s*", text.strip().lower())
    if not m:
        raise ValueError(f"cannot read frequency '{text}' (use forms like 1/7d, 2/day, 1/week)")
    count = int(m.group(1))
    mult = int(m.group(2) or 1)
    unit = m.group(3).rstrip("s")
    if unit not in WINDOW_UNITS:
        raise ValueError(f"unknown time unit '{m.group(3)}' in '{text}'")
    return count, mult * WINDOW_UNITS[unit]


def detect_promotional(rendition: Rendition) -> tuple[bool, list[str]]:
    """True when the post promotes IncuBrix. An explicit flag wins; otherwise
    call-to-action phrases or links to IncuBrix's own domains count."""
    if rendition.promotional is not None:
        return bool(rendition.promotional), (["marked promotional by the sender"] if rendition.promotional else [])
    text = f"{rendition.title}\n{rendition.body}".lower()
    reasons = [f"phrase '{p}'" for p in PROMO_PHRASES if p in text]
    domains = own_domains()
    for url in URL_RE.findall(text) + ([rendition.link] if rendition.link else []):
        host = (urlsplit(url).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in domains):
            reasons.append(f"links to own site {host}")
    return bool(reasons), reasons


def _links(rendition: Rendition) -> list[str]:
    found = URL_RE.findall(rendition.body)
    if rendition.link and rendition.link not in found:
        found.append(rendition.link)
    return found


def _recent_posts(destination_id: str, since: datetime, promotional_only: bool = False) -> list[dict[str, Any]]:
    sql = (
        "SELECT published_at, created_at, state FROM posts WHERE destination_id=? AND is_test=0 "
        "AND state IN ('published','in_flight','awaiting_human') AND COALESCE(published_at, created_at) >= ?"
    )
    if promotional_only:
        sql += " AND promotional=1"
    return get_db().all(sql, [destination_id, since.isoformat(timespec="seconds")])


def check(
    rendition: Rendition,
    destination: Destination,
    *,
    now: datetime | None = None,
    use_ledger: bool = True,
    native: bool = True,
    rules_status: str | None = None,
) -> dict[str, Any]:
    """Return {ok, violations[], warnings[], promotional}."""
    now = now or datetime.now(timezone.utc)
    violations: list[dict[str, Any]] = []
    warnings: list[str] = []

    def block(rule: str, message: str, code: str = "DEST_RULE_VIOLATION", **extra: Any) -> None:
        violations.append({"code": code, "rule": rule, "action": "block", "message": message, **extra})

    if rules_status == "proposed":
        block("rules_unconfirmed", "AI-proposed rules for this destination are waiting for a person to confirm them",
              code="DEST_RULES_UNCONFIRMED")

    # automation
    if native and not destination.automation_allowed:
        block("automation", "this community does not allow automated posting; use the assisted pack so a person posts",
              suggestion="assisted")

    # frequency cap
    try:
        cap = parse_window(destination.frequency_cap or "")
    except ValueError as exc:
        cap = None
        warnings.append(str(exc))
    if cap and use_ledger and destination.id:
        count, seconds = cap
        window_start = now - timedelta(seconds=seconds)
        recent = _recent_posts(destination.id, window_start)
        if len(recent) >= count:
            times = sorted(datetime.fromisoformat(r["published_at"] or r["created_at"]) for r in recent)
            frees_at = times[len(recent) - count] + timedelta(seconds=seconds)
            violations.append({
                "code": "DEST_RULE_FREQUENCY_CAP", "rule": "frequency_cap", "action": "reschedule",
                "message": f"{len(recent)} post(s) already in the last {_human(seconds)}; the cap is "
                           f"{destination.frequency_cap}",
                "reschedule_at": frees_at.isoformat(timespec="seconds"),
            })

    # self-promotion
    promotional, promo_reasons = detect_promotional(rendition)
    policy = (destination.self_promo_allowed or "yes").strip().lower()
    if promotional:
        if policy in ("no", "false", "never", "0"):
            block("self_promotion", "this community bans self-promotion and the post is promotional ("
                  + ", ".join(promo_reasons[:3]) + ")", evidence=promo_reasons)
        elif policy.startswith("limited"):
            limit = parse_window(policy.split(":", 1)[1]) if ":" in policy else (1, 30 * 86400)
            if limit and use_ledger and destination.id:
                count, seconds = limit
                recent = _recent_posts(destination.id, now - timedelta(seconds=seconds), promotional_only=True)
                if len(recent) >= count:
                    times = sorted(datetime.fromisoformat(r["published_at"] or r["created_at"]) for r in recent)
                    violations.append({
                        "code": "DEST_RULE_FREQUENCY_CAP", "rule": "self_promotion_limited", "action": "reschedule",
                        "message": f"self-promotion is limited to {count} per {_human(seconds)} here",
                        "reschedule_at": (times[len(recent) - count] + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
                    })

    # links
    links = _links(rendition)
    for token in [t.strip() for t in (destination.link_policy or "any").split(";") if t.strip()]:
        t = token.lower()
        if t in ("none", "no_links", "no links") and links:
            block("link_policy", "this community does not allow links", links=links[:5])
        elif t == "one_link" and len(links) > 1:
            block("link_policy", f"only one link is allowed here; the post has {len(links)}", links=links[:5])
        elif t == "no_shorteners":
            short = [u for u in links if (urlsplit(u).hostname or "").lower() in SHORTENERS]
            if short:
                block("link_policy", "link shorteners are not allowed here", links=short)
        elif t.startswith("allowlist:"):
            allowed = {d.strip().lower() for d in t.split(":", 1)[1].split(",") if d.strip()}
            bad = [u for u in links if not any((urlsplit(u).hostname or "").lower().endswith(d) for d in allowed)]
            if bad:
                block("link_policy", "links must point to " + ", ".join(sorted(allowed)), links=bad[:5])
        elif t == "canonical_only":
            extra = [u for u in links if u != rendition.canonical_url]
            if extra:
                block("link_policy", "only the canonical article link is allowed here", links=extra[:5])

    # AI disclosure
    if destination.disclosure_required and rendition.ai_generated:
        text = f"{rendition.title}\n{rendition.body}"
        if not DISCLOSURE_RE.search(text) and not (rendition.disclosure and rendition.disclosure in text):
            block("ai_disclosure", "this destination requires AI-generated content to be disclosed and the post "
                  "has no disclosure line (tailoring adds one automatically)")

    # language
    dest_lang = (destination.language or "").lower()[:2]
    content_lang = (rendition.language or "").lower()[:2]
    accepted = {str(x).lower()[:2] for x in destination.config.get("accepted_languages") or []}
    if dest_lang and content_lang and dest_lang != content_lang:
        if content_lang in accepted or destination.config.get("language_strict") is False:
            warnings.append(f"community language is '{dest_lang}' and the post is '{content_lang}'; "
                            "posted because this community accepts it (config.accepted_languages)")
        else:
            # default: posting in the wrong language is a common way to get banned, so hold it
            block("language", f"this community's language is '{dest_lang}' and the post is '{content_lang}'; "
                  f"add content.translations['{dest_lang}'], or list '{content_lang}' in the destination's "
                  "config.accepted_languages if the community accepts it")

    return {"ok": not violations, "violations": violations, "warnings": warnings, "promotional": promotional,
            "promotional_reasons": promo_reasons}


def _human(seconds: int) -> str:
    if seconds % 604800 == 0:
        n = seconds // 604800
        return f"{n} week" + ("s" if n > 1 else "")
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"{n} day" + ("s" if n > 1 else "")
    n = max(1, seconds // 3600)
    return f"{n} hour" + ("s" if n > 1 else "")
