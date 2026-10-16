"""Advanced: catalogue import, rules capture, relevance, tailoring; Outstanding: recommender, metrics, feeds."""
from __future__ import annotations

import io
import time

from tests.conftest import make_destination, publish, wait_job

MESSY_CSV = """name,url,platform_key,language,audience_tags,frequency_cap,self_promo_allowed,community
AI Video Forum,https://forum.aivideo.mock,discourse,en,ai;video,1/7d,limited,
AI Video Forum again,HTTPS://www.forum.aivideo.mock/?utm_source=x,discourse,en,ai,,,
Bad one,not a url,discourse,en,,,,
Spaces,https://exa mple.com,ghost,,,,,
Lemmy KI,https://feddit.mock/c/kuenstliche_intelligenz,lemmy,de,ai,,,
Lemmy without community,https://lemmy.mock,lemmy,,,,,
Comunidad de creadores,https://creadores.mock,discourse,es,creators,,maybe,
Startup Gründer Forum,https://gruender.mock,discourse,xx,founders,2 per week,no,
Reddit NewTubers,https://www.reddit.com/r/NewTubers,reddit,en,video;creators,1/7d,no,
Mystery site,https://mystery.mock,tumblr,en,,,,
"""


def _import(client, csv_text: str) -> dict:
    job = client.post("/api/v1/catalogue/imports/upload",
                      files={"file": ("dest.csv", io.BytesIO(csv_text.encode()), "text/csv")}).json()
    done = wait_job(client, job, timeout=300)
    assert done["status"] == "succeeded", done
    return done["result"]


def test_messy_csv_report(client):
    res = _import(client, MESSY_CSV)
    s = res["summary"]
    rows = {r["row"]: r for r in res["rows_preview"]}
    assert rows[3]["status"] == "duplicate" and rows[3]["duplicate_of_row"] == 2   # normalised URL + platform
    assert rows[4]["status"] == "error"                                              # not a url
    assert rows[5]["status"] == "error"                                              # spaces
    assert rows[7]["status"] == "error"                                              # lemmy without community
    assert rows[8]["status"] == "error" and "self_promo_allowed" in rows[8]["errors"][0]
    assert rows[9]["status"] in ("created", "updated") and any("ISO" in w for w in rows[9]["warnings"])
    assert rows[10]["status"] in ("created", "updated")
    assert any("unknown platform" in w for w in rows[11]["warnings"])
    assert s["duplicates"] == 1 and s["errors"] == 4
    for link in ("report_csv", "report_json", "catalogue_csv"):
        assert res["links"][link].startswith("http")
    # every created row has a 1-5 relevance score and a reason
    for r in res["rows_preview"]:
        if r["status"] in ("created", "updated"):
            assert 1 <= r["relevance_score"] <= 5 and r["relevance_reason"]


def test_thousand_rows_under_five_minutes(client):
    lines = ["name,url,platform_key,audience_tags,language"]
    for i in range(1000):
        plat = ["discourse", "ghost", "reddit", "devto"][(i % 950) % 4]
        lines.append(f"Community {i} for AI video creators,https://c{i % 950}.bulk.mock/{'x' if plat == 'devto' else ''},"
                     f"{plat},ai;video,en")
    t0 = time.time()
    res = _import(client, "\n".join(lines) + "\n")
    took = time.time() - t0
    assert res["summary"]["rows"] == 1000 and res["summary"]["duplicates"] == 50
    assert took < 300, took


def test_rules_capture_needs_human_confirmation(client):
    dest = make_destination(client, "discourse", rules_url="https://rules-test.mock/guidelines")
    job = wait_job(client, client.post(f"/api/v1/destinations/{dest['id']}/rules/capture", json={}).json())
    prop = job["result"]
    p = prop["proposed"]
    assert p["self_promo_allowed"] == "no" and p["frequency_cap"] == "1/7d" and p["link_policy"] == "no_shorteners"
    assert p["disclosure_required"] is True and p["automation_allowed"] is False
    assert all(e["quote"] for e in p["evidence"])
    # not used until confirmed: posting is blocked meanwhile
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "blocked" and any(v["code"] == "DEST_RULES_UNCONFIRMED" for v in r["violations"])
    conf = client.post(f"/api/v1/destinations/{dest['id']}/rules/{prop['proposal_id']}/confirm",
                       json={"decided_by": "reviewer@incubrix", "overrides": {"automation_allowed": True}}).json()
    assert conf["status"] == "confirmed"
    d = conf["destination"]
    assert d["self_promo_allowed"] == "no" and d["frequency_cap"] == "1/7d" and d["automation_allowed"] is True


def test_relevance_scores(client):
    job = wait_job(client, client.post("/api/v1/relevance", json={"destination_ids": ["dst_forum_aicreators",
                                                                                     "dst_mrss_ctv"]}).json())
    for r in job["result"]["results"]:
        assert 1 <= r["score"] <= 5 and r["reason"]


def test_tailoring_styles(client):
    content = {"title": "We shipped AI captions for creators", "body": "Long article body #AI #video.\n\nSecond para.",
               "summary": "AI captions now ship in IncuBrix.", "link": "https://incubrix.com/blog/captions",
               "tags": ["AI", "Video"], "ai_generated": False}
    res = client.post("/api/v1/tailor", json={"content": content, "destination_ids": [
        "dst_forum_aicreators", "dst_devto", "dst_slack_creators", "dst_assist_hn"]}).json()["renditions"]
    forum, devto, slack, hn = res
    assert forum["rendition"]["body"].strip().endswith("More detail: https://incubrix.com/blog/captions")
    assert "#AI" not in forum["rendition"]["body"] and "Curious how others" in forum["rendition"]["body"]
    assert devto["rendition"]["canonical_url"] == "https://incubrix.com/blog/captions"
    assert devto["rendition"]["tags"] == ["ai", "video"]
    assert len(slack["rendition"]["body"]) <= 600
    assert hn["rendition"]["body"] == ""


def test_recommender_ranks_with_reasons_and_excludes_rule_breakers(client):
    content = {"title": "Sign up for IncuBrix: AI video for creators", "body": "Free trial for creators and founders.",
               "promotional": True, "content_type": "video", "tags": ["ai", "video"]}
    res = client.post("/api/v1/recommendations", json={"content": content, "top_k": 5}).json()
    recs = res["recommendations"]
    assert recs and all(r["reason"] for r in recs)
    assert recs == sorted(recs, key=lambda r: -r["score"])
    excluded = {e["destination_id"] for e in res["excluded"]}
    assert "dst_forum_founders" in excluded  # no-promotion forum


def test_fetch_metrics_on_three_or_more_connectors(client):
    ids = []
    for key in ("discourse", "lemmy", "devto", "hashnode", "slack", "webhook"):
        d = make_destination(client, key)
        r = publish(client, [d["id"]], key=f"metrics-{key}")["result"]["results"][0]
        assert r["state"] == "published"
        ids.append(r["post_id"])
    job = wait_job(client, client.post("/api/v1/metrics/refresh", json={"post_ids": ids}).json())
    got = [r for r in job["result"]["results"] if "metrics" in r]
    assert len(got) >= 3
    for r in got:
        assert set(r["metrics"]) >= {"views", "likes", "comments", "shares", "clicks"}


def test_mrss_feed(client):
    content = {"title": "Episode 12: AI color grading", "body": "Grading in minutes.", "content_type": "video",
               "media": [{"url": "https://cdn.example.com/ep12.mp4", "type": "video", "mime": "video/mp4",
                          "duration_s": 312},
                         {"url": "https://cdn.example.com/ep12.jpg", "type": "image", "mime": "image/jpeg"}]}
    r = publish(client, ["dst_mrss_ctv"], content=content, key="mrss-ep12")["result"]["results"][0]
    assert r["state"] == "published"
    xml = client.get("/api/v1/feeds/incubrix-ctv.xml").text
    assert "Episode 12: AI color grading" in xml and 'medium="video"' in xml and 'duration="312"' in xml


def test_assisted_pack_then_person_posts(client):
    r = publish(client, ["dst_assist_hn"], content={"title": "Show HN: open-source caption aligner",
                                                    "body": "", "link": "https://github.com/incubrix/aligner"},
                key="hn-1")["result"]["results"][0]
    assert r["state"] == "awaiting_human"
    pack = r["assist_pack"]
    assert pack["deep_link"].startswith("https://news.ycombinator.com/submitlink?u=")
    done = client.post(f"/api/v1/assist/{pack['pack_id']}/posted",
                       json={"live_url": "https://news.ycombinator.com/item?id=1"}).json()
    assert done["status"] == "posted"
