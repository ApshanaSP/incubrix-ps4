"""Recommender (Outstanding): rank destinations for one content item by fit,
with a reason for each.

score = 0.35 topic + 0.25 audience + 0.20 rules + 0.10 past results + 0.10 format, each 0-1.
  topic     what the post is about against what the destination is about: TF-IDF
            cosine between the post's words (title, tags, summary, body) and the
            destination's name, category, description and relevance reason, scaled
            so the best match in the catalogue is 1. A post about n8n finds the n8n
            forum; a DaVinci Resolve tutorial finds the Blackmagic forum
  audience  overlap between the content's themes and the destination's
            audience tags, plus its relevance score (1-5)
  rules     1 when the post would pass every rule today; promotional content
            to a no-promotion community is excluded outright; a reached
            frequency cap or a needed translation lowers the score
  past      engagement of earlier IncuBrix posts there (from fetch_metrics),
            relative to the best destination; 0.5 when there is no history
  format    whether the destination suits the content type (video -> feeds,
            articles -> blogs, discussions -> forums)
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from app import rules
from app.catalogue import intel, store
from app.connectors.base import Destination
from app.db import get_db
from app.tailoring import ContentItem, tailor

FORMAT_FIT = {
    "video": {"mrss": 1.0, "webhook": 0.7, "lemmy": 0.6, "discourse": 0.5, "slack": 0.6, "assisted": 0.6,
              "devto": 0.3, "hashnode": 0.3, "ghost": 0.5},
    "article": {"devto": 1.0, "hashnode": 1.0, "ghost": 1.0, "webhook": 0.8, "discourse": 0.6, "lemmy": 0.6,
                "slack": 0.5, "assisted": 0.6, "mrss": 0.1},
    "discussion": {"discourse": 1.0, "lemmy": 1.0, "slack": 0.8, "assisted": 0.8, "devto": 0.5, "hashnode": 0.4,
                   "ghost": 0.3, "webhook": 0.4, "mrss": 0.0},
    "announcement": {"slack": 0.9, "webhook": 0.9, "ghost": 0.8, "assisted": 0.8, "devto": 0.6, "hashnode": 0.6,
                     "discourse": 0.5, "lemmy": 0.5, "mrss": 0.4},
}


STOPWORDS = set("""a an and are as at be by for from has have how i in into is it its of on one or our that the their this
to was we what when where which who why with you your about after all also can do does more most new not than them then
there these they using use used via vs will just like get how-to guide week year years ten""".split())
# platform words say where a place is, not what it is about
PLACE_WORDS = {"community", "communities", "forum", "forums", "discord", "slack", "reddit", "lemmy", "discuss",
               "discussion", "discussions", "official", "users", "group", "channel", "tag", "posting", "account",
               "incubrix", "via", "stack", "exchange", "peertube", "instance", "publication", "medium"}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]", (text or "").lower())
    out = []
    for w in words:
        w = w.strip(".")
        if len(w) < 2 or w in STOPWORDS or w in PLACE_WORDS or w.isdigit():
            continue
        out.append(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def _destination_text(row: dict[str, Any]) -> str:
    cfg = row.get("config") or {}
    name = re.sub(r"^(reddit\s+r/|lemmy[.\w]*\s+c/|dev\.to\s+#)", "", row.get("name") or "", flags=re.I)
    return " ".join(str(x) for x in (name, name, row.get("category"), cfg.get("description"),
                                       cfg.get("community"), row.get("relevance_reason")) if x)


def _topic_scores(content: ContentItem, rows: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, list[str]]]:
    """TF-IDF cosine between the post and each destination, scaled so the best is 1, plus the shared words."""
    docs = {r["id"]: Counter(_tokens(_destination_text(r))) for r in rows}
    n = max(1, len(docs))
    df: Counter = Counter()
    for c in docs.values():
        df.update(set(c))
    idf = {w: math.log((n + 1) / (k + 1)) + 1 for w, k in df.items()}
    q_text = " ".join([content.title, content.title, " ".join(content.tags), " ".join(content.tags),
                       content.summary or "", content.body[:3000]])
    q = Counter(w for w in _tokens(q_text) if w in idf)
    qv = {w: tf * idf[w] for w, tf in q.items()}
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    raw: dict[str, float] = {}
    shared: dict[str, list[str]] = {}
    for rid, c in docs.items():
        dv = {w: tf * idf[w] for w, tf in c.items()}
        dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        common = set(qv) & set(dv)
        raw[rid] = sum(qv[w] * dv[w] for w in common) / (qn * dn)
        shared[rid] = sorted(common, key=lambda w: -qv[w] * dv[w])[:4]
    best = max(raw.values(), default=0.0)
    return ({rid: (v / best if best > 0 else 0.0) for rid, v in raw.items()}, shared)


def _past_results() -> dict[str, float]:
    """destination_id -> mean engagement (likes + 2*comments + 3*shares + clicks) per post."""
    rows = get_db().all(
        "SELECT p.destination_id AS d, m.data AS data FROM metrics m JOIN posts p ON p.id = m.post_id WHERE p.is_test=0"
    )
    sums: dict[str, list[float]] = {}
    for r in rows:
        d = r["data"] or {}
        score = d.get("likes", 0) + 2 * d.get("comments", 0) + 3 * d.get("shares", 0) + d.get("clicks", 0)
        sums.setdefault(r["d"], []).append(float(score))
    return {k: sum(v) / len(v) for k, v in sums.items()}


def recommend(content: ContentItem, top_k: int = 10, include_uncertified: bool = False) -> dict[str, Any]:
    themes = set(content.audience_tags) or set(intel.heuristic_relevance(
        f"{content.title} {content.summary or ''} {content.body[:2000]} {' '.join(content.tags)}")["audience_tags"])
    past = _past_results()
    best_past = max(past.values(), default=0.0)
    ranked: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    rows = store.list_destinations()
    topic, shared = _topic_scores(content, rows)
    for row in rows:
        if row["status"] in ("demoted", "excluded", "paused") or (row["status"] != "certified" and not include_uncertified):
            excluded.append({"destination_id": row["id"], "name": row["name"],
                             "reason": f"not eligible (status {row['status']})"})
            continue
        dest: Destination = store.to_model(row)
        reasons: list[str] = []
        topic_score = topic.get(row["id"], 0.0)
        if shared.get(row["id"]) and topic_score >= 0.15:
            reasons.append("topic match: " + ", ".join(shared[row["id"]]))
        # audience
        dtags = set(row.get("audience_tags") or [])
        overlap = themes & dtags
        jacc = len(overlap) / len(themes | dtags) if (themes | dtags) else 0.0
        rel = ((row.get("relevance_score") or 3) - 1) / 4
        audience = 0.6 * rel + 0.4 * (len(overlap) / len(themes) if themes else jacc)
        if overlap:
            reasons.append("audience overlap: " + ", ".join(sorted(overlap)))
        reasons.append(f"relevance {row.get('relevance_score') or '?'}/5")
        # rules, checked on the rendition it would actually get
        rendition, _notes = tailor(content, dest)
        check = rules.check(rendition, dest, native=dest.connector_key != "assisted", rules_status=row.get("rules_status"))
        hard = [v for v in check["violations"] if v["action"] == "block"]
        assisted_route = dest.connector_key != "assisted" and bool(hard) and all(v["rule"] == "automation" for v in hard)
        if assisted_route:
            check = rules.check(rendition, dest, native=False, rules_status=row.get("rules_status"))
            hard = [v for v in check["violations"] if v["action"] == "block"]
        if hard:
            excluded.append({"destination_id": row["id"], "name": row["name"],
                             "reason": "rules: " + "; ".join(v["message"] for v in hard)})
            continue
        rules_score = 1.0
        for v in check["violations"]:
            if v["action"] == "reschedule":
                rules_score -= 0.5
                reasons.append(f"cap reached, next slot {v.get('reschedule_at')}")
        if check["warnings"]:
            rules_score -= 0.2
            reasons.append(check["warnings"][0])
        if dest.connector_key == "assisted":
            reasons.append("a person posts it (assisted)")
        elif assisted_route:
            rules_score -= 0.1
            reasons.append("no automation allowed here: a person posts the ready-made pack")
        # past results
        if row["id"] in past and best_past > 0:
            past_score = math.log1p(past[row["id"]]) / math.log1p(best_past)
            reasons.append(f"past engagement {past[row['id']]:.1f}/post")
        else:
            past_score = 0.5
        fmt = FORMAT_FIT.get(content.content_type, FORMAT_FIT["article"]).get(dest.connector_key, 0.5)
        score = (0.35 * topic_score + 0.25 * audience + 0.20 * max(0.0, rules_score) + 0.10 * past_score
                 + 0.10 * fmt)
        ranked.append({"destination_id": row["id"], "name": row["name"], "connector_key": dest.connector_key,
                       "route": "assisted" if assisted_route or dest.connector_key == "assisted" else "automated",
                       "score": round(score, 4),
                       "components": {"topic": round(topic_score, 3), "audience": round(audience, 3),
                                      "rules": round(max(0.0, rules_score), 3), "past": round(past_score, 3),
                                      "format": round(fmt, 3)},
                       "reason": "; ".join(reasons)})
    ranked.sort(key=lambda r: (-r["score"], r["name"]))
    return {"content_themes": sorted(themes), "recommendations": ranked[:top_k], "considered": len(ranked),
            "excluded": excluded}


def precision_at_k(ranked_ids: list[str], relevant: set[str], k: int = 10) -> float:
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for d in top if d in relevant) / len(top)
