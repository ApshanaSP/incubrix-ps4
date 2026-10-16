"""Must-have 4: rules checked before every post; hard cases 3, 4 and 7."""
from __future__ import annotations

from tests.conftest import make_destination, mock_live_posts, publish


def test_second_post_inside_weekly_cap_is_rescheduled(client):
    dest = make_destination(client, "discourse", frequency_cap="1/7d")
    first = publish(client, [dest["id"]], key="cap-1")["result"]["results"][0]
    assert first["state"] == "published"
    second = publish(client, [dest["id"]], content={"title": "A second, different post inside the same week",
                                                   "body": "Different body text that is long enough."},
                     key="cap-2")["result"]["results"][0]
    assert second["state"] == "rescheduled"
    assert second["code"] == "DEST_RULE_FREQUENCY_CAP"
    assert second["reschedule_at"] and "cap is 1/7d" in second["reason"]
    assert mock_live_posts(client, dest["id"]) == 1


def test_promotional_post_to_no_promotion_community_is_blocked(client):
    dest = make_destination(client, "lemmy", self_promo_allowed="no")
    promo = {"title": "Try IncuBrix free: sign up today", "body": "Get started today with our new tool, 30% off.",
             "link": "https://incubrix.com/pricing"}
    res = publish(client, [dest["id"]], content=promo)["result"]["results"][0]
    assert res["state"] == "blocked"
    assert any(v["rule"] == "self_promotion" for v in res["violations"])
    assert "bans self-promotion" in res["reason"]
    assert mock_live_posts(client, dest["id"]) == 0


def test_explicit_promotional_flag(client):
    dest = make_destination(client, "discourse", self_promo_allowed="no")
    res = publish(client, [dest["id"]], content={"title": "A neutral looking title here", "body": "Neutral body text "
                                                 "without any call to action.", "promotional": True})
    assert res["result"]["results"][0]["state"] == "blocked"


def test_link_policies(client):
    dest = make_destination(client, "discourse", link_policy="no_shorteners;one_link")
    res = publish(client, [dest["id"]], content={"title": "Links that break the policy here",
                                                 "body": "See https://bit.ly/abc and https://example.org/x for more"})
    r = res["result"]["results"][0]
    assert r["state"] == "blocked"
    rules = {v["rule"] for v in r["violations"]}
    assert rules == {"link_policy"}
    assert "shorteners" in r["reason"] and "only one link" in r["reason"]


def test_ai_disclosure_added_by_tailoring(client):
    dest = make_destination(client, "ghost", disclosure_required=True)
    res = publish(client, [dest["id"]], content={"title": "AI-written summary of the creator economy",
                                                 "body": "A summary.", "ai_generated": True})
    r = res["result"]["results"][0]
    assert r["state"] == "published"
    assert "Disclosure: this post was created with the help of AI" in r["rendition"]["body"]
    assert any("disclosure" in n for n in r["tailoring_notes"])


def test_missing_disclosure_blocked_at_contract_validate(client):
    dest = {"name": "x", "url": "https://x.mock", "disclosure_required": True, "config": {"base_url": "https://x.mock"}}
    v = client.post("/connector/v1/discourse/validate", json={
        "rendition": {"title": "Five editing tricks we learned", "body": "No label here at all.", "ai_generated": True},
        "destination": dest}).json()
    assert v["ok"] is False and v["violations"][0]["rule"] == "ai_disclosure"


def test_non_english_community_gets_local_disclosure_and_warning(client):
    res = publish(client, ["dst_lemmy_videomacher"], content={"title": "Five AI tricks for faster video edits",
                                                              "body": "Short guide.", "ai_generated": True,
                                                              "language": "en"}, key="de-1")
    r = res["result"]["results"][0]
    assert r["state"] == "published"
    assert "Hinweis: Dieser Beitrag wurde mit Hilfe von KI erstellt" in r["rendition"]["body"]
    assert any("community language is 'de'" in w for w in r["warnings"])


def test_strict_language_blocks(client):
    dest = make_destination(client, "lemmy", language="ta", config={"language_strict": True})
    r = publish(client, [dest["id"]], content={"title": "English title for a Tamil community",
                                               "body": "English body.", "language": "en"})["result"]["results"][0]
    assert r["state"] == "blocked" and any(v["rule"] == "language" for v in r["violations"])


def test_automation_not_allowed_blocks_native(client):
    dest = make_destination(client, "discourse")
    client.patch(f"/api/v1/destinations/{dest['id']}", json={"automation_allowed": False})
    r = publish(client, [dest["id"]])["result"]["results"][0]
    assert r["state"] == "blocked" and any(v["rule"] == "automation" for v in r["violations"])


def test_too_long_title_is_cut_at_word_boundary(client):
    dest = make_destination(client, "devto")
    long_title = "How creators can use AI " + "and video workflows " * 10
    r = publish(client, [dest["id"]], content={"title": long_title, "body": "Body.", "tags": ["AI", "Video Editing",
                                                                                        "creators", "SaaS", "extra"]})
    out = r["result"]["results"][0]
    assert out["state"] == "published"
    title = out["rendition"]["title"]
    assert len(title) <= 128 and title.endswith("…") and not title[:-1].endswith(" ")
    assert long_title.startswith(title[:-1])
    assert out["rendition"]["tags"] == ["ai", "videoediting", "creators", "saas"]


def test_too_long_at_contract_level_gives_field_reasons(client):
    v = client.post("/connector/v1/lemmy/validate", json={
        "rendition": {"title": "x" * 250, "body": "y" * 10050,
                      "media": [{"url": "https://a/b.pdf", "type": "file"}]},
        "destination": {"url": "https://l.mock", "config": {"base_url": "https://l.mock", "community": "c"}}}).json()
    fields = {x["field"] for x in v["violations"]}
    assert {"title", "body", "media[0]"} <= fields
    assert all(x["code"] == "DEST_SPEC_VIOLATION" for x in v["violations"])


def test_unaccepted_media_is_dropped_with_note(client):
    dest = make_destination(client, "slack")
    r = publish(client, [dest["id"]], content={"title": "Clip of the week", "body": "Watch it.",
                                               "media": [{"url": "https://cdn.example.com/a.mp4", "type": "video"}]})
    out = r["result"]["results"][0]
    assert out["state"] == "published"
    assert any("dropped video media" in n for n in out["tailoring_notes"])
