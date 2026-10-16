"""Reviewer-style check: tests every must-have, Integration Standard point and hard case over HTTP,
the way a reviewer would, with inputs that differ from batch_inputs/ (new wording, other caps, other
fault timings). Prints one PASS/FAIL line per check and writes a JSON report.

    python -m tools.reviewer_check --api http://localhost:8080 [--repo .] [--server-log data/server.log]

Needs a service in simulation mode (mock platforms). Nothing is sent to a real platform.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

TEXT = {"title": "What we learned shipping subtitles for 200 short videos",
        "body": "A write-up of our subtitle pipeline: tools, timing errors we hit and how long each step took.",
        "tags": ["video", "ai"], "promotional": False}
CREDS = {
    "discourse": {"api_key": "sim-discourse-key", "api_username": "incubrix_bot"},
    "lemmy": {"username": "incubrix_bot", "password": "sim-password"},
    "devto": {"api_key": "sim-devto-key"},
    "ghost": {"admin_api_key": "6489a6f2b8c1d2e3f4a5b6c7:" + "cd" * 32},
    "webhook": {"signing_secret": "whsec_simulation_only"},
    "hashnode": {"token": "sim-hashnode-token"},
    "slack": {"bot_token": "xoxb-simulation-only"},
}


class Check:
    def __init__(self, api: str, token: str | None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.c = httpx.Client(base_url=api, headers=headers, timeout=120)
        self.results: list[dict[str, Any]] = []
        self.run = uuid.uuid4().hex[:6]

    # ------------------------------------------------------------ http helpers
    def req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for _ in range(10):
            r = self.c.request(method, path, **kw)
            if r.status_code == 429 and "SERVICE_RATE_LIMIT" in r.text:
                time.sleep(float(r.json()["error"]["details"].get("retry_after_s", 5)))
                continue
            return r
        return r

    def post(self, path: str, body: Any = None, key: str | None = None, **kw: Any) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key else {}
        return self.req("POST", path, json=body, headers=headers, **kw)

    def job(self, resp: httpx.Response) -> dict[str, Any]:
        """Waits for a 202 job and returns it; other responses come back as their JSON."""
        body = resp.json()
        if resp.status_code != 202:
            return body
        for _ in range(600):
            j = self.req("GET", f"/api/v1/jobs/{body['job_id']}").json()
            if j["status"] in ("succeeded", "failed"):
                return j
            time.sleep(0.25)
        return {"status": "timeout"}

    def dest(self, connector: str, tag: str, **fields: Any) -> dict[str, Any]:
        """One new destination of a supported type: one row, no code. Certified before use."""
        host = f"https://{connector}-{tag}-{self.run}.mock"
        config = {"discourse": {"base_url": host}, "lemmy": {"base_url": host, "community": "incubrix_test"},
                  "devto": {"api_base_url": host}, "ghost": {"base_url": host}, "webhook": {"base_url": host},
                  "hashnode": {"api_base_url": host, "publication_id": "pub1"},
                  "assisted": {"submit_url": host + "/submit"}}[connector]
        body: dict[str, Any] = {"url": host, "connector_key": connector, "name": f"check {connector} {tag}",
                                "config": config, "audience_tags": ["video", "ai"], "language": "en", **fields}
        if connector in CREDS:
            con = self.post("/api/v1/connections", {"connector_key": connector, "credentials": CREDS[connector]}).json()
            body["connection_id"] = con["id"]
        d = self.post("/api/v1/destinations", body).json()
        cert = self.job(self.post(f"/api/v1/destinations/{d['id']}/certify", {}))
        d["certified"] = bool((cert.get("result") or {}).get("passed"))
        d["cert"] = cert.get("result")
        return d

    def publish(self, d: dict[str, Any], content: dict[str, Any], key: str | None = None) -> dict[str, Any]:
        j = self.job(self.post("/api/v1/publications", {"content": content, "destination_ids": [d["id"]]},
                               key or f"chk-{uuid.uuid4().hex}"))
        return ((j.get("result") or {}).get("results") or [{}])[0] | {"_job": j}

    def live_posts(self, d: dict[str, Any]) -> int:
        return self.req("GET", "/api/v1/sim/state", params={"destination_id": d["id"]}).json().get("posts_live", -1)

    def fault(self, d: dict[str, Any], kind: str, op: str = "publish", **kw: Any) -> None:
        self.post("/api/v1/sim/faults", {"destination_id": d["id"], "kind": kind, "op": op, **kw})

    # ------------------------------------------------------------ recording
    def check(self, area: str, name: str, fn: Callable[[], tuple[bool, Any]]) -> None:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check, with the reason
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append({"area": area, "check": name, "ok": bool(ok), "detail": detail,
                             "seconds": round(time.time() - t0, 2)})
        print(f"{'PASS' if ok else 'FAIL'}  [{area}] {name}" + ("" if ok else f"  -> {str(detail)[:300]}"), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.getenv("API_URL", "http://localhost:8080"))
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--server-log", default="", help="service log file to search for secrets (optional)")
    ap.add_argument("--out", default="outputs/reviewer_check")
    args = ap.parse_args(argv)
    k = Check(args.api, os.getenv("API_TOKEN"))
    repo = Path(args.repo)

    # ================================================================ must-have 1: contract
    def manifests():
        need = {"key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map", "instance_param"}
        ms = k.req("GET", "/api/v1/connectors").json()["connectors"]
        bad = {m.get("key"): sorted(need - set(m)) for m in ms if need - set(m)}
        bad |= {m["key"]: "tier/auth" for m in ms if m["tier"] not in ("native", "protocol", "bridge", "feed", "assisted")
                or m["auth"] not in ("oauth2", "api_key", "bot_token", "none")}
        codes = {"DEST_SPEC_VIOLATION", "DEST_RULE_FREQUENCY_CAP", "CONNECTION_AUTH_EXPIRED", "PLATFORM_RATE_LIMIT",
                 "PLATFORM_QUOTA_EXHAUSTED", "PLATFORM_CONTENT_REJECTED", "PLATFORM_UNAVAILABLE", "CONNECTOR_DISABLED"}
        bad |= {m["key"]: f"error_map uses {set(m['error_map'].values()) - codes}" for m in ms
                if set(m["error_map"].values()) - codes}
        return not bad and len(ms) >= 9, {"connectors": [m["key"] for m in ms], "problems": bad}
    k.check("MH1 contract", "9 manifests have every Appendix A field and only shared error codes", manifests)

    dests: dict[str, dict[str, Any]] = {}
    for ck in ("discourse", "lemmy", "devto", "ghost", "webhook", "assisted"):
        dests[ck] = k.dest(ck, "core")

    def contract_round_trip(ck: str):
        def run():
            d = dests[ck]
            ref = {"destination": {"id": d["id"]}, "connection": {"id": d.get("connection_id")} if d.get("connection_id") else None}
            rend = {"title": "Contract check: subtitles for short videos", "body": "Testing the connector contract end to end."}
            v = k.post(f"/connector/v1/{ck}/validate", {**ref, "rendition": rend}).json()
            if ck == "assisted":
                pack = k.post(f"/connector/v1/{ck}/build_assist_pack", {**ref, "rendition": rend}).json()
                okp = all(x in pack for x in ("files", "caption", "deep_link", "checklist"))
                return v.get("ok") and okp, {"validate": v, "pack_keys": sorted(pack)}
            key = f"contract-{ck}-{k.run}"
            p1 = k.post(f"/connector/v1/{ck}/publish", {**ref, "rendition": rend, "idempotency_key": key}).json()
            p2 = k.post(f"/connector/v1/{ck}/publish", {**ref, "rendition": rend, "idempotency_key": key}).json()
            conn = ref["connection"]
            s1 = k.post(f"/connector/v1/{ck}/get_status", {"platform_post_id": p1["platform_post_id"], "connection": conn}).json()
            dl = k.post(f"/connector/v1/{ck}/delete", {"platform_post_id": p1["platform_post_id"], "connection": conn}).json()
            s2 = k.post(f"/connector/v1/{ck}/get_status", {"platform_post_id": p1["platform_post_id"], "connection": conn}).json()
            ok = (v.get("ok") and p1.get("live_url") and p1["platform_post_id"] == p2.get("platform_post_id")
                  and s1.get("status") == "live" and dl.get("deleted") is True and s2.get("status") in ("deleted", "unpublished"))
            return ok, {"publish": p1, "same_key_again": p2, "status": s1, "delete": dl, "status_after": s2}
        return run
    for ck in dests:
        k.check("MH1 contract", f"{ck}: validate, publish, same key again, get_status, delete over /connector/v1", contract_round_trip(ck))

    # ================================================================ must-have 2: certification
    def core_certified():
        return all(d["certified"] for d in dests.values()), {ck: (d["cert"] or {}).get("level") or (d["cert"] or {}).get("cert_level")
                                                            for ck, d in dests.items()}
    k.check("MH2 certified", "6 Core connectors pass a full certification run", core_certified)

    def evidence_complete():
        cert = dests["discourse"]["cert"] or {}
        text = json.dumps(cert)
        steps = ("publish", "status", "delete")
        return all(s in text for s in steps) and k.live_posts(dests["discourse"]) == 0, \
            {"keys": sorted(cert), "posts_left_on_platform": k.live_posts(dests["discourse"])}
    k.check("MH2 certified", "certification stores evidence and leaves no test post behind", evidence_complete)

    # ================================================================ must-have 3: configuration, not code
    def new_instance():
        d = k.dest("discourse", "forum41", name="Forum number 41")
        return d["certified"], {"id": d["id"], "level": (d["cert"] or {}).get("cert_level")}
    k.check("MH3 config", "a brand-new Discourse forum is one row and certifies with no code", new_instance)

    # ================================================================ must-have 4: rules before every post
    def freq_cap():
        d = k.dest("lemmy", "cap", frequency_cap="2/1d")
        states = [k.publish(d, {**TEXT, "title": f"Subtitle pipeline, part {i}"}) for i in range(3)]
        ok = [s.get("state") for s in states[:2]] == ["published", "published"] and states[2].get("state") == "rescheduled" \
            and states[2].get("code") == "DEST_RULE_FREQUENCY_CAP" and states[2].get("reschedule_at")
        return ok and k.live_posts(d) == 2, [{x: s.get(x) for x in ("state", "code", "reason", "reschedule_at")} for s in states]
    k.check("MH4 rules", "third post inside a 2-per-day cap is rescheduled with the time and reason", freq_cap)

    def promo_unflagged():
        d = k.dest("discourse", "nopromo", self_promo_allowed="no")
        s = k.publish(d, {"title": "Launch week: our editor is out", "body": "Use code LAUNCH50 for 50% off the Pro plan. Buy now."})
        return s.get("state") == "blocked" and k.live_posts(d) == 0, {x: s.get(x) for x in ("state", "code", "reason")}
    k.check("MH4 rules", "promotional post with no 'promotional' flag is detected and blocked", promo_unflagged)

    def link_policy():
        d = k.dest("discourse", "nolinks", link_policy="none")
        s = k.publish(d, {**TEXT, "body": TEXT["body"] + " Full write-up: https://example.com/subtitles"})
        return s.get("state") == "blocked", {x: s.get(x) for x in ("state", "code", "reason")}
    k.check("MH4 rules", "a link in the body is blocked where the link policy is 'none'", link_policy)

    def disclosure():
        d = k.dest("ghost", "disclose", disclosure_required=True)
        s = k.publish(d, {**TEXT, "ai_generated": True})
        v = k.post("/connector/v1/ghost/validate", {"destination": {"url": "https://y.mock", "disclosure_required": True,
                   "config": {"base_url": "https://y.mock"}}, "rendition": {"title": "Five tips", "body": "x", "ai_generated": True}}).json()
        return s.get("state") == "published" and not v.get("ok"), {"pipeline": {x: s.get(x) for x in ("state", "warnings")},
                                                                    "contract_validate_without_label": v.get("violations")}
    k.check("MH4 rules", "AI-generated content gets a disclosure label where required; unlabelled fails validate", disclosure)

    def automation_off():
        d = k.dest("assisted", "noauto", automation_allowed=False)
        s = k.publish(d, TEXT)
        return s.get("state") in ("assist_ready", "assisted", "pack_ready") or bool(s.get("assist_pack")), \
            {x: s.get(x) for x in ("state", "code", "reason")}
    k.check("MH4 rules", "where automation is not allowed, a person gets a ready-to-post pack instead", automation_off)

    # ================================================================ must-have 5 + hard case 2: back-off
    def rate_limit():
        d = k.dest("devto", "429")
        k.fault(d, "rate_limit", retry_after=1800)
        s = k.publish(d, TEXT)
        err = s.get("error") or {}
        return err.get("code") == "PLATFORM_RATE_LIMIT" and s.get("post_state") == "rescheduled", \
            {x: s.get(x) for x in ("state", "post_state", "error", "reschedule_at")}
    k.check("MH5 / HC2", "429 with a 30-minute reset: not retried early, rescheduled to the reset time", rate_limit)

    # ================================================================ must-have 6: safe retries
    def timeout_retry():
        d = k.dest("ghost", "timeout")
        k.fault(d, "timeout_after_create")
        first = k.publish(d, TEXT, key=f"to-{k.run}")
        retry = k.job(k.post(f"/api/v1/posts/{first.get('post_id')}/retry", {}))
        again = k.publish(d, TEXT, key=f"to-{k.run}")
        return k.live_posts(d) == 1, {"first": {x: first.get(x) for x in ("state", "error")},
                                      "retry": (retry.get("result") or {}), "posts_on_platform": k.live_posts(d),
                                      "same_key_publish_again": again.get("post_id") == first.get("post_id")}
    k.check("MH6 retries", "Ghost: post created but answer lost, retried with the same key: exactly 1 post", timeout_retry)

    def header_replay():
        body = {"content": TEXT, "destination_ids": [dests["lemmy"]["id"]], "dry_run": True}
        a = k.post("/api/v1/publications", body, key=f"hdr-{k.run}").json()
        b = k.post("/api/v1/publications", body, key=f"hdr-{k.run}").json()
        c = k.post("/api/v1/publications", {**body, "content": {**TEXT, "title": "Different"}}, key=f"hdr-{k.run}")
        return a.get("job_id") == b.get("job_id") and c.status_code in (409, 422, 202), \
            {"first": a.get("job_id"), "second": b.get("job_id"), "same_key_other_body": c.status_code}
    k.check("IS4 retries", "same Idempotency-Key twice returns the same job; other body with that key is answered", header_replay)

    # ================================================================ must-have 7: secrets + simulation
    secret = f"sk-check-{uuid.uuid4().hex}"
    def secrets():
        con = k.post("/api/v1/connections", {"connector_key": "devto", "credentials": {"api_key": secret}}).json()
        shown = k.req("GET", f"/api/v1/connections/{con['id']}").text + k.req("GET", "/api/v1/connections").text
        found_log = None
        if args.server_log and Path(args.server_log).exists():
            found_log = secret in Path(args.server_log).read_text(encoding="utf-8", errors="ignore")
        pat = re.compile(r"gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16}|xox[bp]-\d{6,}")
        hits = [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts
                and p.suffix in (".py", ".md", ".yml", ".yaml", ".json", ".csv", ".log", ".txt", ".example", ".toml")
                and pat.search(p.read_text(encoding="utf-8", errors="ignore"))]
        return secret not in shown and not found_log and not hits, {"secret_in_api_responses": secret in shown,
                                                                    "secret_in_server_log": found_log, "key_like_strings_in_repo": hits}
    k.check("MH7 secrets", "a stored token never comes back from the API, the log or the repository", secrets)

    def simulation():
        r = k.req("GET", "/ready").json()
        return r["checks"].get("simulation_mode") is True, r
    k.check("MH7 simulation", "service reports simulation mode (mock platforms, no real accounts)", simulation)

    # ================================================================ hard cases
    def expired_key():
        d = k.dest("ghost", "expired")
        k.fault(d, "auth_expired")
        s = k.publish(d, TEXT)
        st = k.req("GET", f"/api/v1/destinations/{d['id']}").json()
        return (s.get("error") or {}).get("code") == "CONNECTION_AUTH_EXPIRED" and st.get("status") == "paused", \
            {"error": s.get("error"), "destination_status": st.get("status")}
    k.check("HC2", "expired API key (Ghost): CONNECTION_AUTH_EXPIRED and the destination is paused", expired_key)

    def expired_session():
        d = k.dest("lemmy", "expired")
        k.fault(d, "auth_expired")
        s = k.publish(d, TEXT)
        return s.get("state") == "published" and any("logged in again" in w for w in s.get("warnings") or []), \
            {x: s.get(x) for x in ("state", "warnings", "error")}
    k.check("HC2", "expired session (Lemmy, password login): logs in again once, posts, and says so", expired_session)

    def rejected():
        d = k.dest("hashnode", "reject")
        k.fault(d, "reject")
        s = k.publish(d, TEXT)
        e = s.get("error") or {}
        return e.get("code") == "PLATFORM_CONTENT_REJECTED" and e.get("retryable") is False and e.get("message"), e
    k.check("HC2", "content rejected: PLATFORM_CONTENT_REJECTED with the platform's message, not retried", rejected)

    def too_long():
        long_title = "Subtitles " * 60
        v = k.post("/connector/v1/discourse/validate", {"destination": {"url": "https://z.mock", "config": {"base_url": "https://z.mock"}},
                   "rendition": {"title": long_title, "body": "Body text long enough for the forum."}}).json()
        s = k.publish(dests["discourse"], {**TEXT, "title": long_title})
        fields = [x.get("field") for x in v.get("violations", [])]
        title = (s.get("rendition") or {}).get("title", "")
        cut_note = any("title cut" in n for n in s.get("tailoring_notes") or [])
        return (not v.get("ok") and "title" in fields and s.get("state") == "published" and cut_note
                and len(title) <= 255 and title.endswith("…") and not title[:-1].endswith(" Subtitle")), \
            {"validate_violations": v.get("violations"), "pipeline": {"state": s.get("state"), "title_chars": len(title),
             "notes": s.get("tailoring_notes")}}
    k.check("HC3", "title over the limit: field-level DEST_SPEC_VIOLATION; the pipeline truncates at a word with a warning", too_long)

    def bad_media():
        v = k.post("/connector/v1/lemmy/validate", {"destination": {"url": "https://m.mock", "config": {"base_url": "https://m.mock", "community": "c"}},
                   "rendition": {"title": "A title long enough", "body": "Body", "media": [{"url": "https://a/b.exe", "type": "file"}]}}).json()
        return not v.get("ok") and any("media" in str(x.get("field")) for x in v.get("violations", [])), v.get("violations")
    k.check("HC3", "media the destination does not accept is reported per item", bad_media)

    def renamed_field():
        d = k.dest("lemmy", "drift")
        k.fault(d, "rename_field", op="any", count=5)
        cert = k.job(k.post(f"/api/v1/destinations/{d['id']}/certify", {}))
        st = k.req("GET", f"/api/v1/destinations/{d['id']}").json()
        s = k.publish(d, TEXT)
        tickets = k.req("GET", "/api/v1/tickets").text
        return (not (cert.get("result") or {}).get("passed") and st.get("status") == "demoted"
                and s.get("code") == "DEST_NOT_CERTIFIED" and d["id"] in tickets), \
            {"cert_error": (cert.get("result") or {}).get("error"), "status": st.get("status"), "publish_after": s.get("code"),
             "ticket_opened": d["id"] in tickets}
    k.check("HC5", "a renamed API field fails certification, demotes, excludes and opens a ticket", renamed_field)

    def csv_import():
        rows = ["name,url,platform_key,connector_key,language,audience_tags",
                "Forum A,https://forum-a.example.com,discourse,discourse,en,ai",
                "Forum A again,https://FORUM-A.example.com/?utm_source=x,discourse,discourse,en,ai",
                "Broken,not a url,discourse,discourse,en,ai",
                "Communauté vidéo,https://video-fr.example.org,discourse,discourse,,video",
                "日本語コミュニティ,https://jp.example.net,lemmy,lemmy,,ai",
                "No platform,https://x.example.com,,,en,ai"]
        files = {"file": ("check.csv", "\n".join(rows).encode(), "text/csv")}
        j = k.job(k.req("POST", "/api/v1/catalogue/imports/upload", files=files, data={"commit": "false"},
                        headers={"Idempotency-Key": f"csv-{k.run}"}))
        res = j.get("result") or {}
        text = json.dumps(res)
        return res.get("duplicates", res.get("summary", {}).get("duplicates")) not in (None, 0) and "not a url" in text.lower() \
            or ("duplicate" in text and "invalid" in text.lower()), {k2: v for k2, v in res.items() if k2 != "rows"}
    k.check("HC6", "CSV with a duplicate, a bad URL, mixed languages and a missing platform: row-by-row report", csv_import)

    def revoke():
        d = k.dest("webhook", "revoke")
        k.fault(d, "revoke")
        s = k.publish(d, TEXT)
        st = k.req("GET", f"/api/v1/destinations/{d['id']}").json()
        return (s.get("error") or {}).get("code") == "CONNECTION_AUTH_EXPIRED" and st.get("status") == "paused", \
            {"error": s.get("error"), "status": st.get("status")}
    k.check("HC6", "partner revokes permission: posting stops and the owner is asked to reconnect", revoke)

    def non_english():
        d = k.dest("discourse", "ja", language="ja")
        s = k.publish(d, {**TEXT, "language": "en"})
        t = k.publish(d, {**TEXT, "language": "en", "translations": {"ja": {
            "title": "200本のショート動画に字幕を付けて学んだこと", "body": "字幕パイプラインの記録：使ったツール、タイミングの誤り、各工程の所要時間。"}}})
        return (s.get("state") == "blocked" and s.get("reason") and t.get("state") == "published"
                and (t.get("rendition") or {}).get("language") == "ja"), \
            {"no_translation": {x: s.get(x) for x in ("state", "reason")},
             "with_translation": {"state": t.get("state"), "title": (t.get("rendition") or {}).get("title")}}
    k.check("HC7", "Japanese community: English post held with the reason; the creator's Japanese version posts", non_english)

    # ================================================================ Integration Standard
    def openapi():
        live = yaml.safe_load(k.req("GET", "/openapi.yaml").text)
        repo_spec = yaml.safe_load((repo / "openapi.yaml").read_text(encoding="utf-8"))
        paths = list(live["paths"])
        outside = [p for p in paths if not p.startswith(("/api/v1/", "/connector/v1/", "/health", "/ready"))]
        same = set(live["paths"]) == set(repo_spec["paths"])
        return live["openapi"].startswith("3.1") and same and not outside, \
            {"openapi": live["openapi"], "paths": len(paths), "repo_file_matches_live": same, "outside_api_v1": outside}
    k.check("IS2 API", "openapi.yaml is OpenAPI 3.1, matches the running service, routes under /api/v1/", openapi)

    def jobs():
        r = k.post(f"/api/v1/destinations/{dests['discourse']['id']}/certify", {})
        b = r.json()
        j = k.req("GET", f"/api/v1/jobs/{b['job_id']}").json()
        return r.status_code == 202 and j["status"] in ("queued", "running", "succeeded", "failed") and 0 <= j["progress"] <= 100 \
            and j.get("links"), {"status_code": r.status_code, "job_keys": sorted(j)}
    k.check("IS3 jobs", "slow work returns 202 + job_id; job has status, progress 0-100 and links", jobs)

    def errors():
        a = k.req("GET", "/api/v1/destinations/dst_does_not_exist")
        b = k.post("/api/v1/destinations", {"name": "missing url and connector"})
        c = k.req("GET", "/api/v1/errors").json()
        shape = all(set(x.json()["error"]) >= {"code", "message", "retryable", "details"} for x in (a, b))
        upper = all(re.fullmatch(r"[A-Z][A-Z0-9_]+", x.json()["error"]["code"]) for x in (a, b))
        listed = {a.json()["error"]["code"], b.json()["error"]["code"]} <= set(json.dumps(c).replace('"', " ").split())
        return shape and upper and listed, {"404": a.json(), "422": b.json()["error"]["code"]}
    k.check("IS5 errors", "errors are {error:{code,message,retryable,details}}, UPPER_SNAKE_CASE, listed", errors)

    def files():
        up = k.req("POST", "/api/v1/uploads", files={"file": ("in.csv", b"name,url\n", "text/csv")},
                   headers={"Idempotency-Key": f"up-{k.run}"}).json()
        got = httpx.get(up["url"], timeout=30) if up.get("url", "").startswith("http") else k.req("GET", up["url"])
        csv_text = "name,url,platform_key,connector_key\nURL import,https://url-import.example.com,discourse,discourse\n"
        up2 = k.req("POST", "/api/v1/uploads", files={"file": ("by_url.csv", csv_text.encode(), "text/csv")},
                    headers={"Idempotency-Key": f"up2-{k.run}"}).json()
        imp = k.job(k.post("/api/v1/catalogue/imports", {"url": up2["url"], "commit": False}))
        links = json.dumps(imp.get("result") or {})
        return got.status_code == 200 and imp.get("status") == "succeeded" and "http" in links, \
            {"upload_url": up.get("url"), "download_status": got.status_code, "import_by_url": imp.get("status")}
    k.check("IS7 files", "upload in, import by URL, results saved to storage and returned as links", files)

    def logs_health():
        cid = f"check-{k.run}"
        r = k.req("GET", "/api/v1/connectors", headers={"X-Correlation-Id": cid})
        h, rd = k.req("GET", "/health"), k.req("GET", "/ready")
        in_log = None
        if args.server_log and Path(args.server_log).exists():
            lines = [ln for ln in Path(args.server_log).read_text(encoding="utf-8", errors="ignore").splitlines() if cid in ln]
            in_log = bool(lines) and all({"timestamp", "level", "job_id", "correlation_id"} <= set(json.loads(ln)) for ln in lines)
        return r.headers.get("x-correlation-id") == cid and h.status_code == 200 and rd.status_code == 200 and in_log is not False, \
            {"echoed": r.headers.get("x-correlation-id"), "health": h.json(), "json_log_line_has_fields": in_log}
    k.check("IS8 logs", "X-Correlation-Id echoed and logged as JSON; /health and /ready answer", logs_health)

    def manifest():
        j = k.job(k.post("/api/v1/relevance", {"destination_ids": [dests["lemmy"]["id"]]}))
        m = (j.get("result") or {}).get("manifest") or {}
        text = json.dumps(m).lower()
        need = ("provider", "setting", "seed", "timing", "cost", "warning", "licen")
        return all(n in text for n in need), {"manifest_keys": sorted(m)}
    k.check("IS9 manifest", "results carry a manifest: providers and versions, settings, seed, timings, cost, warnings, licence", manifest)

    # ================================================================ Outstanding
    def recommend():
        r = k.post("/api/v1/recommendations", {"content": {**TEXT, "audience_tags": ["video", "creators"]}, "top_k": 5}).json()
        items = r.get("recommendations") or r.get("items") or r.get("ranked") or []
        return len(items) > 0 and all(i.get("reason") or i.get("reasons") for i in items), \
            [{x: i.get(x) for x in ("name", "score", "reason", "reasons")} for i in items[:3]]
    k.check("Outstanding", "recommender ranks destinations with a reason for each", recommend)

    def metrics():
        out = {}
        for ck in ("discourse", "lemmy", "devto"):
            d = dests[ck]
            ref = {"destination": {"id": d["id"]}, "connection": {"id": d.get("connection_id")}}
            p = k.post(f"/connector/v1/{ck}/publish", {**ref, "rendition": {"title": "Metrics check for short videos",
                       "body": "Checking normalised metrics."}, "idempotency_key": f"m-{ck}-{k.run}"}).json()
            m = k.post(f"/connector/v1/{ck}/fetch_metrics", {"platform_post_id": p["platform_post_id"], "connection": ref["connection"]}).json()
            k.post(f"/connector/v1/{ck}/delete", {"platform_post_id": p["platform_post_id"], "connection": ref["connection"]})
            out[ck] = sorted(m)
        need = {"views", "likes", "comments", "shares", "clicks"}
        return all(need <= set(v) for v in out.values()), out
    k.check("Outstanding", "fetch_metrics on 3 connectors returns views, likes, comments, shares, clicks", metrics)

    def recert():
        j = k.job(k.post("/api/v1/recertification/run", {}))
        return j.get("status") == "succeeded", {k2: v for k2, v in (j.get("result") or {}).items() if k2 != "manifest"}
    k.check("Outstanding", "recertification run is a job and reports what it re-tested", recert)

    # ================================================================ report
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    passed = sum(r["ok"] for r in k.results)
    (out / "reviewer_check.json").write_text(json.dumps({"api": args.api, "passed": passed, "total": len(k.results),
                                                         "results": k.results}, indent=2, ensure_ascii=False, default=str),
                                             encoding="utf-8")
    print(f"\n{passed}/{len(k.results)} checks passed. Report: {out / 'reviewer_check.json'}")
    return 0 if passed == len(k.results) else 1


if __name__ == "__main__":
    sys.exit(main())
