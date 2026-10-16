"""Writes batch_inputs/*.json: one input per hard case and per Core/Advanced feature.
Each input creates its own destinations (hosts contain {{run}}), so runs never collide.

    python -m tools.make_batch_inputs
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "batch_inputs"

CREDS = {
    "discourse": {"api_key": "sim-discourse-key", "api_username": "incubrix_bot"},
    "lemmy": {"username": "incubrix_bot", "password": "sim-password"},
    "devto": {"api_key": "sim-devto-key"},
    "ghost": {"admin_api_key": "6489a6f2b8c1d2e3f4a5b6c7:" + "cd" * 32},
    "webhook": {"signing_secret": "whsec_simulation_only"},
    "hashnode": {"token": "sim-hashnode-token"},
    "slack": {"bot_token": "xoxb-sim-1111-batch"},
}
ALL = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted", "hashnode", "slack", "mrss"]
CONTENT = {"title": "How we cut video editing time with AI captions",
           "body": "We tested three captioning workflows for short videos and measured the time each one saved.",
           "tags": ["ai", "video"], "promotional": False}


def ref(name: str) -> str:
    return "{{" + name + "}}"


def cfg(c: str, host: str) -> dict:
    return {"discourse": {"base_url": f"https://{host}"},
            "lemmy": {"base_url": f"https://{host}", "community": "incubrix_test"},
            "devto": {"api_base_url": f"https://{host}"}, "ghost": {"base_url": f"https://{host}"},
            "webhook": {"base_url": f"https://{host}"},
            "hashnode": {"api_base_url": f"https://{host}", "publication_id": "pub1"},
            "slack": {"api_base_url": f"https://{host}", "channel": "C0BATCH"},
            "mrss": {"feed_key": "batch-" + ref("run")},
            "assisted": {"submit_url": f"https://{host}/submit"}}[c]


def new_dest(c: str, tag: str, config: dict | None = None, **fields) -> list[dict]:
    host = f"{c}-{tag}-{ref('run')}.mock"
    steps = []
    body = {"url": f"https://{host}", "connector_key": c, "name": f"batch {c} {tag}",
            "config": config or cfg(c, host), "audience_tags": ["ai", "creators"], "language": "en", **fields}
    if c in CREDS:
        steps.append({"method": "POST", "path": "/api/v1/connections",
                      "body": {"connector_key": c, "credentials": CREDS[c]}, "save_as": f"con_{tag}",
                      "expect_status": 201})
        body["connection_id"] = ref(f"con_{tag}.id")
    steps.append({"method": "POST", "path": "/api/v1/destinations", "body": body, "save_as": f"dest_{tag}",
                  "expect_status": 201})
    steps.append({"method": "POST", "path": f"/api/v1/destinations/{ref(f'dest_{tag}.id')}/certify", "body": {},
                  "save_as": f"cert_{tag}", "expect": {"result.passed": True}})
    return steps


def pub(tag: str, content: dict | None = None, key: str | None = None, save: str = "pub", **expect) -> dict:
    step = {"method": "POST", "path": "/api/v1/publications",
            "body": {"content": content or CONTENT, "destination_ids": [ref(f"dest_{tag}.id")]},
            "headers": {"Idempotency-Key": key or f"{save}-{ref('run')}"}, "save_as": save}
    if expect:
        step["expect"] = expect
    return step


def fault(tag: str, kind: str, op: str = "publish", **kw) -> dict:
    return {"method": "POST", "path": "/api/v1/sim/faults",
            "body": {"destination_id": ref(f"dest_{tag}.id"), "kind": kind, "op": op, **kw}, "expect_status": 200}


def state(tag: str, live: int) -> dict:
    return {"method": "GET", "path": "/api/v1/sim/state", "params": {"destination_id": ref(f"dest_{tag}.id")},
            "expect": {"posts_live": live}}


def get_dest(tag: str, **expect) -> dict:
    return {"method": "GET", "path": f"/api/v1/destinations/{ref(f'dest_{tag}.id')}", "expect": expect}


def build() -> dict[str, dict]:
    cases: dict[str, dict] = {}
    steps: list[dict] = []
    for c in ALL:
        steps += new_dest(c, c)
    steps.append({"method": "POST", "path": "/api/v1/publications", "headers": {"Idempotency-Key": "all-" + ref("run")},
                  "body": {"content": {**CONTENT, "media": [{"url": "https://cdn.example.com/ep.mp4", "type": "video",
                                                             "mime": "video/mp4"}]},
                           "destination_ids": [ref(f"dest_{c}.id") for c in ALL]},
                  "save_as": "pub", "expect": {"status": "succeeded"}})
    cases["01_certify_and_publish_every_connector"] = {
        "description": "Certify a fresh destination for each of the 9 connectors, then publish one item to all of them.",
        "steps": steps}
    cases["02_timeout_then_retry_same_key"] = {
        "description": "Hard case 1: the platform creates the post but the answer times out; the retry with the same "
                       "key returns that post; exactly one post exists.",
        "steps": new_dest("discourse", "t") + [
            fault("t", "timeout_after_create"), pub("t", key="timeout-" + ref("run"), save="first"),
            {"method": "POST", "path": "/api/v1/posts/" + ref("first.result.results.0.post_id") + "/retry", "body": {},
             "save_as": "retry", "expect": {"result.replayed": True}},
            state("t", 1)]}
    cases["03_rate_limit_429_with_reset"] = {
        "description": "Hard case 2a: a 429 with a 15-minute reset is not retried blindly; the post is rescheduled to the reset time.",
        "steps": new_dest("discourse", "r") + [
            fault("r", "rate_limit", retry_after=900),
            pub("r", save="p", **{"result.results.0.error.code": "PLATFORM_RATE_LIMIT",
                                  "result.results.0.post_state": "rescheduled"})]}
    cases["04_rate_limit_short_backoff"] = {
        "description": "Hard case 2a': a 429 with a 2-second reset: the connector waits for the reset, then succeeds.",
        "steps": new_dest("slack", "s") + [fault("s", "rate_limit", retry_after=2),
                                            pub("s", save="p", **{"result.results.0.state": "published"})]}
    cases["05_expired_token"] = {
        "description": "Hard case 2b: an expired token pauses the destination and asks the owner to reconnect.",
        "steps": new_dest("ghost", "g") + [fault("g", "auth_expired"),
                                            pub("g", save="p", **{"result.results.0.error.code": "CONNECTION_AUTH_EXPIRED"}),
                                            get_dest("g", status="paused")]}
    cases["06_content_rejected"] = {
        "description": "Hard case 2c: the platform rejects the content; its message is returned for review.",
        "steps": new_dest("devto", "d") + [fault("d", "reject"),
                                            pub("d", save="p", **{"result.results.0.error.code": "PLATFORM_CONTENT_REJECTED"})]}
    long_item = {"title": "How creators can use AI " + "and video workflows " * 12, "body": "Body " * 3000,
                 "tags": ["AI", "Video Editing", "creators", "SaaS", "extra"]}
    cases["07_too_long_title_and_body"] = {
        "description": "Hard case 3a: a title and body longer than allowed are cut at a word boundary, with a note.",
        "steps": new_dest("lemmy", "l") + [pub("l", content=long_item, save="p", **{"result.results.0.state": "published"})]}
    cases["08_unsupported_media"] = {
        "description": "Hard case 3b: media the destination does not accept is dropped with a note; contract validate "
                       "reports it per field.",
        "steps": new_dest("slack", "m") + [
            pub("m", content={**CONTENT, "media": [{"url": "https://cdn.example.com/a.mp4", "type": "video"}]}, save="p",
                **{"result.results.0.state": "published"}),
            {"method": "POST", "path": "/connector/v1/discourse/validate",
             "body": {"rendition": {"title": "A title that is long enough", "body": "Body text long enough here",
                                    "media": [{"url": "https://a/b.pdf", "type": "file"}]},
                      "destination": {"url": "https://x.mock", "config": {"base_url": "https://x.mock"}}},
             "expect": {"ok": False, "violations.0.field": "media[0]"}}]}
    cases["09_second_post_inside_weekly_cap"] = {
        "description": "Hard case 4a: the second post inside a 1/7d cap is rescheduled to when the window frees up.",
        "steps": new_dest("discourse", "c", frequency_cap="1/7d") + [
            pub("c", save="p1", **{"result.results.0.state": "published"}),
            pub("c", content={"title": "A second, different post in the same week", "body": "Different body text."},
                save="p2", **{"result.results.0.state": "rescheduled", "result.results.0.code": "DEST_RULE_FREQUENCY_CAP"}),
            state("c", 1)]}
    cases["10_promo_to_no_promotion_community"] = {
        "description": "Hard case 4b: a promotional post to a no-self-promotion community is blocked with the reason.",
        "steps": new_dest("lemmy", "n", self_promo_allowed="no") + [
            pub("n", content={"title": "Try IncuBrix free: sign up today",
                              "body": "Get started today with our new tool, 30% off.",
                              "link": "https://incubrix.com/pricing"}, save="p", **{"result.results.0.state": "blocked"}),
            state("n", 0)]}
    cases["11_renamed_api_field"] = {
        "description": "Hard case 5: the platform renames a field; certification fails, the destination is demoted and "
                       "excluded, and a ticket is opened.",
        "steps": new_dest("devto", "f") + [
            fault("f", "rename_field"),
            {"method": "POST", "path": f"/api/v1/destinations/{ref('dest_f.id')}/certify", "body": {}, "save_as": "recert",
             "expect": {"result.passed": False, "result.error.details.cause": "SCHEMA_DRIFT"}},
            get_dest("f", status="demoted"),
            pub("f", save="p", **{"result.results.0.code": "DEST_NOT_CERTIFIED"})]}
    cases["12_csv_duplicates_bad_urls_mixed_languages"] = {
        "description": "Hard case 6a: a CSV with duplicates, bad URLs and mixed languages gives a row-by-row report.",
        "steps": [{"method": "POST", "path": "/api/v1/catalogue/imports/upload", "upload": "files/messy_destinations.csv",
                   "form": {"commit": "true"}, "save_as": "imp",
                   "expect": {"status": "succeeded", "result.summary.duplicates": 1, "result.summary.errors": 4}}]}
    cases["13_partner_revokes_permission"] = {
        "description": "Hard case 6b: the partner revokes permission (403); the connection is marked revoked and the "
                       "destination paused.",
        "steps": new_dest("webhook", "w") + [fault("w", "revoke"),
                                              pub("w", save="p", **{"result.results.0.error.code": "CONNECTION_AUTH_EXPIRED"}),
                                              get_dest("w", status="paused")]}
    cases["14_non_english_community"] = {
        "description": "Hard case 7a: a German community gets the creator's German translation with a German "
                       "disclosure line; a Tamil community with no Tamil version holds the English post with the reason.",
        "steps": new_dest("lemmy", "de", language="de", disclosure_required=True) + [
            pub("de", content={**CONTENT, "ai_generated": True, "language": "en", "translations": {"de": {
                "title": "Wie wir mit KI-Untertiteln Schnittzeit gespart haben",
                "body": "Wir haben drei Untertitel-Abläufe für kurze Videos getestet und die gesparte Zeit gemessen."}}},
                save="p", **{"result.results.0.state": "published", "result.results.0.rendition.language": "de"})]
        + new_dest("lemmy", "ta", language="ta")
        + [pub("ta", content={**CONTENT, "language": "en"}, save="p2", **{"result.results.0.state": "blocked"})]}
    cases["15_ai_disclosure_required"] = {
        "description": "Hard case 7b: AI content to a destination that requires disclosure gets the disclosure line; "
                       "without it, contract validate blocks.",
        "steps": new_dest("ghost", "ai", disclosure_required=True) + [
            pub("ai", content={**CONTENT, "ai_generated": True}, save="p", **{"result.results.0.state": "published"}),
            {"method": "POST", "path": "/connector/v1/ghost/validate",
             "body": {"rendition": {"title": "Five editing tricks", "body": "No label.", "ai_generated": True},
                      "destination": {"url": "https://x.mock", "disclosure_required": True,
                                      "config": {"base_url": "https://x.mock"}}},
             "expect": {"ok": False, "violations.0.rule": "ai_disclosure"}}]}
    cases["16_rules_capture_needs_confirmation"] = {
        "description": "Advanced: an AI step reads the rules page and proposes rules; posting is blocked until a person "
                       "confirms them.",
        "steps": new_dest("discourse", "rc", rules_url=f"https://rules-{ref('run')}.mock/guidelines") + [
            {"method": "POST", "path": f"/api/v1/destinations/{ref('dest_rc.id')}/rules/capture", "body": {},
             "save_as": "cap", "expect": {"result.proposed.self_promo_allowed": "no"}},
            pub("rc", save="p", **{"result.results.0.state": "blocked"}),
            {"method": "POST", "path": f"/api/v1/destinations/{ref('dest_rc.id')}/rules/{ref('cap.result.proposal_id')}/confirm",
             "body": {"decided_by": "batch-runner", "overrides": {"automation_allowed": True}},
             "expect": {"status": "confirmed"}}]}
    cases["17_recommend_destinations"] = {
        "description": "Outstanding: rank destinations for one content item, with reasons; rule-breakers are excluded.",
        "steps": [{"method": "POST", "path": "/api/v1/recommendations",
                   "body": {"content": {"title": "Sign up for IncuBrix: AI video for creators", "body": "Free trial.",
                                        "promotional": True, "content_type": "video", "tags": ["ai", "video"]},
                            "top_k": 10}}]}
    return cases


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, case in build().items():
        case = {"name": name, **case}
        (OUT / f"{name}.json").write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(build())} inputs to {OUT}")


if __name__ == "__main__":
    main()
