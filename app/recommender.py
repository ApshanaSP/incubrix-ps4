"""Recommender (Outstanding): rank destinations for one content item by fit,
with a reason for each.

score = 0.45 audience + 0.25 rules + 0.20 past results + 0.10 format, each 0-1.
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
    for row in store.list_destinations():
        if row["status"] in ("demoted", "excluded", "paused") or (row["status"] != "certified" and not include_uncertified):
            excluded.append({"destination_id": row["id"], "name": row["name"],
                             "reason": f"not eligible (status {row['status']})"})
            continue
        dest: Destination = store.to_model(row)
        reasons: list[str] = []
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
        # past results
        if row["id"] in past and best_past > 0:
            past_score = math.log1p(past[row["id"]]) / math.log1p(best_past)
            reasons.append(f"past engagement {past[row['id']]:.1f}/post")
        else:
            past_score = 0.5
        fmt = FORMAT_FIT.get(content.content_type, FORMAT_FIT["article"]).get(dest.connector_key, 0.5)
        score = 0.45 * audience + 0.25 * max(0.0, rules_score) + 0.20 * past_score + 0.10 * fmt
        ranked.append({"destination_id": row["id"], "name": row["name"], "connector_key": dest.connector_key,
                       "score": round(score, 4),
                       "components": {"audience": round(audience, 3), "rules": round(max(0.0, rules_score), 3),
                                      "past": round(past_score, 3), "format": round(fmt, 3)},
                       "reason": "; ".join(reasons)})
    ranked.sort(key=lambda r: (-r["score"], r["name"]))
    return {"content_themes": sorted(themes), "recommendations": ranked[:top_k], "considered": len(ranked),
            "excluded": excluded}


def precision_at_k(ranked_ids: list[str], relevant: set[str], k: int = 10) -> float:
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for d in top if d in relevant) / len(top)
