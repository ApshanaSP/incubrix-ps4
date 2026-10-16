"""Measures the numbers for the Metrics tab, in simulation mode, in-process (no keys, no network).

  certification   destinations certified per connector, time per run (N runs each)
  reliability     publishes with injected faults (timeouts after create, 429s, 500s, rejections);
                  each failure is retried with POST /posts/{id}/retry (same idempotency key);
                  success rate, and duplicate posts found on the mock platforms (must be 0)
  rule safety     catalogue/rule_test_set.json: violations caught vs missed, false blocks
  catalogue       1,000-row import time (offline reachability) and 5,000 rows

    python -m tools.measure [--runs 5] [--publishes 300]
Writes outputs/metrics/metrics.json and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import statistics
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="ibx-measure-")
os.environ.update({"DATA_DIR": TMP, "SIMULATION_MODE": "true", "SCHEDULER_ENABLED": "false", "AI_PROVIDER": "mock",
                   "HTTP_TIMEOUT_S": "1.0", "MAX_BACKOFF_S": "3", "REACHABILITY_MODE": "offline",
                   "RATE_LIMIT_PER_MINUTE": "0", "LOG_LEVEL": "ERROR", "PUBLIC_BASE_URL": "http://testserver",
                   "S3_ENDPOINT": "", "MOCK_BASE_URL": "", "API_TOKEN": ""})

from fastapi.testclient import TestClient  # noqa: E402

from app.config import reload_settings  # noqa: E402

reload_settings()
from app.main import app  # noqa: E402
from app.mocks import server as mocks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CREDS = {
    "discourse": {"api_key": "sim-discourse-key", "api_username": "incubrix_bot"},
    "lemmy": {"username": "incubrix_bot", "password": "sim-password"},
    "devto": {"api_key": "sim-devto-key"},
    "ghost": {"admin_api_key": "6489a6f2b8c1d2e3f4a5b6c7:" + "cd" * 32},
    "webhook": {"signing_secret": "whsec_simulation_only"},
    "hashnode": {"token": "sim-hashnode-token"},
    "slack": {"bot_token": "xoxb-sim-1111-measure"},
}
CONNECTORS = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted", "hashnode", "slack", "mrss"]


def wait(c: TestClient, job: dict, timeout: float = 120) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/v1/jobs/{job['job_id']}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.02)
    raise RuntimeError("job timeout")


def new_dest(c: TestClient, key: str, **fields) -> dict:
    host = f"{key}-{uuid.uuid4().hex[:6]}.mock"
    cfg = {"discourse": {"base_url": f"https://{host}"}, "lemmy": {"base_url": f"https://{host}", "community": "c"},
           "devto": {"api_base_url": f"https://{host}"}, "ghost": {"base_url": f"https://{host}"},
           "webhook": {"base_url": f"https://{host}"}, "hashnode": {"api_base_url": f"https://{host}", "publication_id": "p"},
           "slack": {"api_base_url": f"https://{host}", "channel": "C1"}, "mrss": {"feed_key": f"f-{host}"},
           "assisted": {"submit_url": f"https://{host}/submit"}}[key]
    cfg.update(fields.pop("config", {}))
    body = {"url": f"https://{host}", "connector_key": key, "config": cfg, "language": "en", **fields}
    if key in CREDS:
        body["connection_id"] = c.post("/api/v1/connections", json={"connector_key": key, "credentials": CREDS[key]}).json()["id"]
    return c.post("/api/v1/destinations", json=body).json()


def measure_certification(c: TestClient, runs: int) -> dict:
    out = {}
    for key in CONNECTORS:
        times, passed = [], 0
        for _ in range(runs):
            d = new_dest(c, key)
            res = wait(c, c.post(f"/api/v1/destinations/{d['id']}/certify", json={}).json())["result"]
            times.append(res["duration_s"])
            passed += int(res["passed"])
        out[key] = {"destinations_certified": passed, "runs": runs, "mean_s": round(statistics.mean(times), 3),
                    "max_s": round(max(times), 3)}
    return out


def measure_reliability(c: TestClient, n: int, seed: int = 7) -> dict:
    rng = random.Random(seed)
    native = ["discourse", "lemmy", "devto", "ghost", "webhook", "hashnode", "slack"]
    dests = {k: [new_dest(c, k) for _ in range(3)] for k in native}
    for k, ds in dests.items():
        for d in ds:
            wait(c, c.post(f"/api/v1/destinations/{d['id']}/certify", json={}).json())
    faults = [None] * 6 + ["timeout_after_create", "timeout", "rate_limit", "server_error", "reject"]
    stats: Counter = Counter()
    by_fault: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, list[str]] = defaultdict(list)
    for i in range(n):
        key = native[i % len(native)]
        d = rng.choice(dests[key])
        fault = rng.choice(faults)
        if fault:
            c.post("/api/v1/sim/faults", json={"destination_id": d["id"], "kind": fault, "op": "publish",
                                                "retry_after": 1 if rng.random() < 0.5 else 600})
        title = f"Reliability run {i} about AI video workflows"
        titles[d["id"]].append(title)
        res = wait(c, c.post("/api/v1/publications", headers={"Idempotency-Key": f"rel-{i}"},
                             json={"content": {"title": title, "body": "Measured body text for reliability.",
                                               "promotional": False}, "destination_ids": [d["id"]]}).json())
        r = res["result"]["results"][0]
        attempts = 1
        while r.get("state") not in ("published",) and attempts < 4:
            pid = r.get("post_id")
            err = (r.get("error") or {})
            if not pid or err.get("code") in ("PLATFORM_CONTENT_REJECTED",):
                break
            if err.get("code") == "PLATFORM_RATE_LIMIT":
                stats["rescheduled_by_429"] += 1
                break
            attempts += 1
            r = wait(c, c.post(f"/api/v1/posts/{pid}/retry", json={}).json())["result"]
        state = r.get("state")
        stats["total"] += 1
        stats[f"final_{state}"] += 1
        by_fault[fault or "none"][state] += 1
        if fault == "reject":
            stats["rejected_expected"] += 1
    # duplicates: the same title live more than once on any mock instance
    dupes = 0
    for inst in mocks.INSTANCES.values():
        seen = Counter(p.get("title") or p.get("name") or (p.get("text", "")[:60]) for p in inst.posts.values()
                       if not p.get("deleted"))
        dupes += sum(v - 1 for t, v in seen.items() if v > 1 and t and "Reliability run" in str(t))
    eligible = stats["total"] - stats["rejected_expected"] - stats["rescheduled_by_429"]
    return {"publishes": stats["total"], "published": stats["final_published"],
            "rejected_by_platform_as_injected": stats["rejected_expected"],
            "rescheduled_after_long_429": stats["rescheduled_by_429"],
            "success_rate_excluding_injected_rejections_and_reschedules": round(stats["final_published"] / eligible, 4) if eligible else None,
            "duplicate_posts": dupes, "by_fault": {k: dict(v) for k, v in by_fault.items()}}


def measure_rules(c: TestClient) -> dict:
    cases = json.loads((ROOT / "catalogue" / "rule_test_set.json").read_text(encoding="utf-8"))["cases"]
    tp = fn = fp = tn = 0
    misses, false_blocks = [], []
    for case in cases:
        d = new_dest(c, "discourse", **case["destination"])
        wait(c, c.post(f"/api/v1/destinations/{d['id']}/certify", json={}).json())
        for prior in case.get("prior_posts", []):
            wait(c, c.post("/api/v1/publications", json={"content": prior, "destination_ids": [d["id"]]}).json())
        res = wait(c, c.post("/api/v1/publications", json={"content": case["content"], "destination_ids": [d["id"]]}).json())
        state = res["result"]["results"][0]["state"]
        stopped = state in ("blocked", "rescheduled")
        if case["expect_violation"]:
            tp += stopped
            fn += not stopped
            if not stopped:
                misses.append(case["id"])
        else:
            fp += stopped
            tn += not stopped
            if stopped:
                false_blocks.append(case["id"])
    return {"cases": len(cases), "violations": tp + fn, "caught": tp, "missed": fn, "missed_ids": misses,
            "allowed_cases": tn + fp, "false_blocks": fp, "false_block_ids": false_blocks}


def measure_import(c: TestClient) -> dict:
    out = {}
    for n in (1000, 5000):
        lines = ["name,url,platform_key,audience_tags,language"]
        for i in range(n):
            plat = ["discourse", "ghost", "reddit", "devto"][(i % (n - n // 20)) % 4]
            lines.append(f"Community {i},https://c{i % (n - n // 20)}.bulk-{n}.example/,{plat},ai;video,en")
        lines += ["Bad,not a url,discourse,,en", "Bad2,https://exa mple.com,ghost,,en"]
        t0 = time.time()
        job = c.post("/api/v1/catalogue/imports/upload", files={"file": ("d.csv", "\n".join(lines).encode())}).json()
        res = wait(c, job, timeout=600)["result"]["summary"]
        out[f"{n}_rows"] = {"seconds": round(time.time() - t0, 2), "rows": res["rows"], "created": res["created"],
                            "duplicates": res["duplicates"], "errors": res["errors"],
                            "reachability": "offline (network checks skipped)"}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--publishes", type=int, default=300)
    args = ap.parse_args()
    t0 = time.time()
    with TestClient(app) as c:
        result = {
            "hardware": f"{platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs, {platform.system()} {platform.release()}, Python {platform.python_version()}",
            "mode": "simulation (mock platforms in-process), AI provider: mock",
            "certification": measure_certification(c, args.runs),
            "reliability": measure_reliability(c, args.publishes),
            "rule_safety": measure_rules(c),
            "catalogue_import": measure_import(c),
            "cost": {"publish_path_ai_calls": 0, "usd_per_1000_publications": 0.0,
                     "note": "publishing, tailoring and rules checks use no paid API; see REPORT.md for AI rules capture cost"},
        }
    result["seconds_total"] = round(time.time() - t0, 1)
    out = ROOT / "outputs" / "metrics"
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
