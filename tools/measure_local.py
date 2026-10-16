"""Publish reliability on the real local instances (docker compose: Lemmy 0.19 and Ghost 5).

For each local destination: N publishes with distinct titles, each repeated once with the same
Idempotency-Key (must replay, never post twice), the live URL fetched, then every post deleted
through the API. Writes outputs/metrics/local_instances.json.

    python -m tools.measure_local --api http://localhost:8080 [--n 10]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def wait(c: httpx.Client, job: dict, timeout: float = 120) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/v1/jobs/{job['job_id']}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.2)
    raise RuntimeError("job timeout")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()
    headers = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}
    c = httpx.Client(base_url=args.api, headers=headers, timeout=60)
    dests = [d for d in c.get("/api/v1/destinations").json()["destinations"] if d["name"].startswith("Local ")]
    out = {"api": args.api, "per_destination": {}}
    run = uuid.uuid4().hex[:6]
    for d in dests:
        saved_cap = d.get("frequency_cap")
        c.patch(f"/api/v1/destinations/{d['id']}", json={"frequency_cap": ""})  # measuring delivery, not the cap
        times, published, replays_same, live_ok, deleted = [], 0, 0, 0, 0
        post_ids = []
        try:
            for i in range(args.n):
                key = f"local-{run}-{d['id']}-{i}"
                body = {"content": {"title": f"Local delivery check {run} number {i}",
                                    "body": "Automated delivery check on a local test instance; deleted right after.",
                                    "promotional": False}, "destination_ids": [d["id"]]}
                t0 = time.time()
                r1 = wait(c, c.post("/api/v1/publications", json=body, headers={"Idempotency-Key": key}).json())
                times.append(time.time() - t0)
                res = r1["result"]["results"][0]
                if res.get("state") == "published":
                    published += 1
                    post_ids.append(res["post_id"])
                    # the same request again must return the same post
                    r2 = c.post("/api/v1/publications", json=body, headers={"Idempotency-Key": key}).json()
                    if r2["job_id"] == r1["job_id"]:
                        replays_same += 1
                    retry = wait(c, c.post(f"/api/v1/posts/{res['post_id']}/retry", json={}).json())["result"]
                    if retry.get("platform_post_id") != res["platform_post_id"]:
                        replays_same -= 1
                    try:
                        if httpx.get(res["live_url"].replace("lemmy-ui:1234", "localhost:1236")
                                     .replace("ghost:2368", "localhost:2368"), timeout=20).status_code == 200:
                            live_ok += 1
                    except httpx.HTTPError:
                        pass
        finally:
            for pid in post_ids:
                dj = wait(c, c.post(f"/api/v1/posts/{pid}/delete", json={}).json())
                deleted += int(bool((dj.get("result") or {}).get("deleted")))
            c.patch(f"/api/v1/destinations/{d['id']}", json={"frequency_cap": saved_cap or ""})
        out["per_destination"][d["name"]] = {
            "publishes": args.n, "published": published, "success_rate": round(published / args.n, 3),
            "idempotent_replays_returning_same_post": replays_same, "live_url_http_200": live_ok,
            "deleted_after": deleted, "mean_publish_job_s": round(statistics.mean(times), 3) if times else None,
        }
    certs = c.get("/api/v1/certifications", params={"limit": 500}).json()["certifications"]
    local_ids = {d["id"]: d["name"] for d in dests}
    out["certifications"] = [{"destination": local_ids[x["destination_id"]], "status": x["status"],
                              "duration_s": x["duration_s"], "evidence_url": x["evidence_url"]}
                             for x in certs if x["destination_id"] in local_ids]
    dest = ROOT / "outputs" / "metrics"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "local_instances.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
