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

# any one of these makes a post promotional (unless negated: "no sign up needed")
PROMO_PHRASES = [
    "sign up", "signup", "free trial", "try it free", "try it now", "buy now", "promo code", "coupon", "use code",
    "limited time", "today only", "this week only", "subscribe now", "book a demo", "get started today",
    "check out our", "check out my", "our product", "our platform", "our new tool", "our tool", "our app",
    "our editor", "our service", "early access", "join our waitlist", "upgrade now", "link in bio",
    "we just launched", "we launched", "we just shipped", "we shipped", "now available",
    "affiliate", "commission", "referral link", "ref link", "sponsored",
]
# these alone are a topic, not an ad ("why 50% off sales hurt creators"); they count only with a link or a phrase above
WEAK_PROMO_PHRASES = ["discount", "% off", "pricing", "on sale", "deal"]
AFFILIATE_LINK_RE = re.compile(r"(amzn\.to/|amazon\.[a-z.]+/.*[?&]tag=|[?&](ref|aff|affiliate|via)=|/ref/|/aff/)", re.I)
NEGATION_RE = re.compile(r"\b(no|not|without|never|don't|doesn't|no need to)\W+(?:\w+\W+){0,2}$", re.I)

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

    def said(phrase: str) -> bool:
        # the phrase appears and is not negated just before it ("no sign up needed", "without a free trial")
        for m in re.finditer(r"(?<![a-z])" + re.escape(phrase), text):
            if not NEGATION_RE.search(text[max(0, m.start() - 30):m.start()]):
                return True
        return False

    reasons = [f"phrase '{p}'" for p in PROMO_PHRASES if said(p)]
    links = URL_RE.findall(text) + ([rendition.link] if rendition.link else [])
    domains = own_domains()
    for url in links:
        if _host_in(url, domains):
            reasons.append(f"links to own site {(urlsplit(url).hostname or '').lower()}")
        if AFFILIATE_LINK_RE.search(url):
            reasons.append(f"affiliate-style link {url[:60]}")
    for d in domains:  # the own site named without a link ("find us at incubrix.com")
        if re.search(r"(?<![\w.])" + re.escape(d) + r"(?![\w])", text) and not any(f"own site" in r for r in reasons):
            reasons.append(f"names own site {d}")
    weak = [p for p in WEAK_PROMO_PHRASES if said(p)]
    if weak and (reasons or links):
        reasons += [f"phrase '{p}'" for p in weak]
    return bool(reasons), reasons


def _links(rendition: Rendition) -> list[str]:
    found = URL_RE.findall(f"{rendition.title}\n{rendition.body}")
    if rendition.link and rendition.link not in found:
        found.append(rendition.link)
    return found


def _host_in(url: str, domains: set[str]) -> bool:
    """The link's host is one of the domains or a subdomain of one ("evilexample.com" is not "example.com")."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _recent_posts(destination_id: str, since: datetime, promotional_only: bool = False,
                  exclude_post_id: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT published_at, created_at, state FROM posts WHERE destination_id=? AND is_test=0 "
        "AND state IN ('published','in_flight','awaiting_human') AND COALESCE(published_at, created_at) >= ?"
    )
    params: list[Any] = [destination_id, since.isoformat(timespec="seconds")]
    if promotional_only:
        sql += " AND promotional=1"
    if exclude_post_id:
        sql += " AND id != ?"
        params.append(exclude_post_id)
    return get_db().all(sql, params)


def check(
    rendition: Rendition,
    destination: Destination,
    *,
    now: datetime | None = None,
    use_ledger: bool = True,
    native: bool = True,
    rules_status: str | None = None,
    exclude_post_id: str | None = None,
) -> dict[str, Any]:
    """Return {ok, violations[], warnings[], promotional}. `exclude_post_id` is the post being retried, so its own
    earlier attempt is not counted against the caps."""
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
        recent = _recent_posts(destination.id, window_start, exclude_post_id=exclude_post_id)
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
            try:
                limit = parse_window(policy.split(":", 1)[1]) if ":" in policy else (1, 30 * 86400)
            except ValueError as exc:
                # an unreadable limit must not let promotion through: fall back to the strict default
                limit = (1, 30 * 86400)
                warnings.append(f"self_promo_allowed: {exc}; using 1 promotional post per 30 days")
            if limit and use_ledger and destination.id:
                count, seconds = limit
                recent = _recent_posts(destination.id, now - timedelta(seconds=seconds), promotional_only=True,
                                       exclude_post_id=exclude_post_id)
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
            short = [u for u in links if _host_in(u, SHORTENERS)]
            if short:
                block("link_policy", "link shorteners are not allowed here", links=short)
        elif t.startswith("allowlist:"):
            allowed = {d.strip().lower().removeprefix("www.") for d in t.split(":", 1)[1].split(",") if d.strip()}
            bad = [u for u in links if not _host_in(u, allowed)]
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
