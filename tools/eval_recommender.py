"""Precision of the recommender's top 10 on a labelled set (Outstanding).

Inputs:
  catalogue/real_catalogue.csv           the destinations (catalogue only, nothing is posted)
  catalogue/recommender_eval_items.json  content items
  catalogue/recommender_labels.csv       content_id,destination_name: the destinations a person judged
                                         a good place for that item
The catalogue is imported into a fresh in-process service (simulation, offline reachability); each item
is ranked over the whole catalogue (include_uncertified, because catalogue rows are not certified) and
precision@10 = labelled-relevant destinations in the top 10 / 10.

    python -m tools.eval_recommender
Writes outputs/metrics/recommender.json.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="ibx-reco-")
os.environ.update({"DATA_DIR": TMP, "SIMULATION_MODE": "true", "SCHEDULER_ENABLED": "false", "AI_PROVIDER": "mock",
                   "REACHABILITY_MODE": "offline", "RATE_LIMIT_PER_MINUTE": "0", "LOG_LEVEL": "ERROR",
                   "CERTIFY_ON_START": "false", "SEED_DEMO": "false", "PUBLIC_BASE_URL": "http://testserver",
                   "S3_ENDPOINT": "", "API_TOKEN": ""})

from fastapi.testclient import TestClient  # noqa: E402

from app.config import reload_settings  # noqa: E402

reload_settings()
from app.main import app  # noqa: E402
from app.recommender import precision_at_k  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CAT = ROOT / "catalogue"


def main() -> None:
    items = json.loads((CAT / "recommender_eval_items.json").read_text(encoding="utf-8"))["items"]
    labels: dict[str, set[str]] = defaultdict(set)
    for row in csv.DictReader(open(CAT / "recommender_labels.csv", encoding="utf-8")):
        labels[row["content_id"]].add(row["destination_name"].strip())
    with TestClient(app) as c:
        # demo seed rows would compete with the real catalogue; remove them
        from app.db import get_db

        get_db().execute("DELETE FROM destinations")
        job = c.post("/api/v1/catalogue/imports/upload",
                     files={"file": ("real.csv", (CAT / "real_catalogue.csv").read_bytes(), "text/csv")}).json()
        while c.get(f"/api/v1/jobs/{job['job_id']}").json()["status"] not in ("succeeded", "failed"):
            time.sleep(0.1)
        names = {r["id"]: r["name"] for r in c.get("/api/v1/destinations").json()["destinations"]}
        known = set(names.values())
        per_item = []
        for it in items:
            res = c.post("/api/v1/recommendations", json={"content": it["content"], "top_k": 10,
                                                          "include_uncertified": True}).json()
            top = [r["name"] for r in res["recommendations"]]
            rel = labels[it["id"]] & known
            p = precision_at_k(top, rel, 10)
            per_item.append({"id": it["id"], "title": it["content"]["title"], "precision_at_10": round(p, 2),
                             "labelled_relevant_in_catalogue": len(rel), "top10": top,
                             "top10_hits": [t for t in top if t in rel],
                             "excluded_by_rules": len(res["excluded"])})
    mean = sum(x["precision_at_10"] for x in per_item) / len(per_item)
    out = {"items": len(per_item), "catalogue_size": len(known), "mean_precision_at_10": round(mean, 3),
           "per_item": per_item,
           "unknown_label_names": sorted({n for s in labels.values() for n in s} - known)}
    dest = ROOT / "outputs" / "metrics"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "recommender.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("items", "catalogue_size", "mean_precision_at_10", "unknown_label_names")}))
    for x in per_item:
        print(f"  P@10={x['precision_at_10']:.1f}  {x['title']}")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
