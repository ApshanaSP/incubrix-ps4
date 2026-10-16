"""Online catalogue-import timings over HTTP (Advanced: 1,000 rows in under 5 minutes, row-by-row report).

    python -m tools.measure_import_online --api http://localhost:8080

1. Imports catalogue/real_catalogue.csv with live reachability checks (robots.txt respected).
2. Builds a 1,000-row file: the real rows, 50 duplicates (same URL with other case, "www." and tracking
   parameters), 10 bad URLs, and filler rows on *.invalid hosts (RFC 2606: they never resolve, so no real
   site is contacted, but each row still goes through the full reachability check), then imports it.
Both imports run as jobs with commit=false, so the catalogue is not changed. Writes
outputs/metrics/catalogue_import_online.json.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def run_import(c: httpx.Client, name: str, data: bytes) -> dict:
    t0 = time.time()
    r = c.post("/api/v1/catalogue/imports/upload", files={"file": (name, data, "text/csv")}, data={"commit": "false"},
               headers={"Idempotency-Key": f"measure-{name}-{time.time()}"})
    r.raise_for_status()
    job_id = r.json()["job_id"]
    while True:
        job = c.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            break
        time.sleep(1)
    res = job.get("result") or {}
    return {"file": name, "status": job["status"], "seconds": round(time.time() - t0, 1),
            **{k: v for k, v in res.items() if k not in ("rows", "manifest")}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.getenv("API_URL", "http://localhost:8080"))
    args = ap.parse_args()
    headers = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}
    real_path = ROOT / "catalogue" / "real_catalogue.csv"
    real = list(csv.DictReader(open(real_path, encoding="utf-8")))
    fields = list(real[0].keys())
    out: dict = {"api": args.api, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with httpx.Client(base_url=args.api, headers=headers, timeout=60) as c:
        out["real_catalogue"] = run_import(c, "real_catalogue.csv", real_path.read_bytes())
        print("real catalogue:", json.dumps(out["real_catalogue"])[:400], flush=True)

        rows = [dict(r) for r in real]
        for r in real[:50]:
            dup = dict(r)
            scheme, rest = dup["url"].split("://", 1)
            host, _, path = rest.partition("/")
            host = host if host.startswith("www.") else "www." + host
            # same page: other case in the host, "www.", a trailing slash and a tracking parameter
            dup["url"] = f"{scheme.upper()}://{host.upper()}/{path.rstrip('/')}/" + ("&" if "?" in path else "?")                 + "utm_source=newsletter"
            dup["name"] = r["name"] + " (duplicate)"
            rows.append(dup)
        for i in range(10):
            rows.append({**{f: "" for f in fields}, "name": f"Bad URL {i}", "url": f"htp:/broken url {i}",
                         "platform_key": "discourse", "connector_key": "discourse"})
        i = 0
        while len(rows) < 1000:
            i += 1
            rows.append({**{f: "" for f in fields}, "name": f"Filler forum {i}", "url": f"https://forum-{i}.example.invalid",
                         "platform_key": "discourse", "connector_key": "discourse", "language": "en",
                         "audience_tags": "ai;video"})
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
        out["rows_1000"] = run_import(c, "rows_1000.csv", buf.getvalue().encode())
        out["rows_1000"]["built_from"] = (f"{len(real)} real rows + 50 duplicates + 10 bad URLs + "
                                          f"{1000 - len(real) - 60} rows on .invalid hosts")
        print("1,000 rows:", json.dumps(out["rows_1000"])[:400], flush=True)
    dest = ROOT / "outputs" / "metrics" / "catalogue_import_online.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
