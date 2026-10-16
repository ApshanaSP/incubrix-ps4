"""Builds certification_pack/: for every connector, one destination certified over the API, with
  run.json    the full run record (publish, live URL check, status, metrics, delete, deletion confirmed)
  card.html   the same record as a page, and card.png, a screenshot of it (Microsoft Edge, headless)
plus the real local Lemmy and Ghost runs (screenshots of the real posts in docs/evidence/) and INDEX.md.

    python -m tools.make_cert_pack --api http://localhost:8080

The mock platforms have no web pages, so for simulated destinations the screenshot shows the stored
evidence, and every card says "simulated". Nothing is posted to a real community.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from tools.reviewer_check import CREDS

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "certification_pack"
CONNECTORS = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted", "hashnode", "slack", "mrss"]
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "microsoft-edge", "chromium", "google-chrome"]


def config_for(ck: str, host: str) -> dict[str, Any]:
    return {"discourse": {"base_url": host}, "lemmy": {"base_url": host, "community": "incubrix_test"},
            "devto": {"api_base_url": host}, "ghost": {"base_url": host}, "webhook": {"base_url": host},
            "hashnode": {"api_base_url": host, "publication_id": "pub1"},
            "slack": {"api_base_url": host, "channel": "C0CERT"}, "mrss": {"feed_key": f"cert-{uuid.uuid4().hex[:6]}"},
            "assisted": {"submit_url": host + "/submit"}}[ck]


def card(ck: str, dest: dict[str, Any], run: dict[str, Any]) -> str:
    rows = []
    for s in run.get("steps", []):
        res = json.dumps(s.get("result"), ensure_ascii=False)
        rows.append(f"<tr class='{'ok' if s.get('ok') else 'bad'}'><td>{html.escape(s['step'])}</td>"
                    f"<td>{'yes' if s.get('ok') else 'NO'}</td><td>{html.escape(s.get('at', ''))}</td>"
                    f"<td><code>{html.escape(res[:420])}</code></td></tr>")
    verdict = "PASSED" if run.get("passed") else "FAILED"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{ck} certification</title><style>
body{{font:14px/1.45 Segoe UI,Arial,sans-serif;margin:24px;color:#1a1a1a;background:#fff}}
h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#555;margin-bottom:14px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;color:#fff;
background:{'#1f7a3d' if run.get('passed') else '#b3261e'}}} .sim{{background:#6b5b00}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}}
th{{background:#f3f3f3}} tr.bad td{{background:#fdecea}} code{{font:12px Consolas,monospace;word-break:break-all}}
</style></head><body>
<h1>Certification: {html.escape(dest['name'])} <span class="badge">{verdict}</span>
<span class="badge sim">{'simulated platform' if run.get('simulated') else 'real platform'}</span></h1>
<div class="meta">connector <b>{ck}</b> {html.escape(str(run.get('connector_version', '')))} &middot; level
<b>{html.escape(str(run.get('level')))}</b> &middot; destination {html.escape(dest['id'])} &middot; {html.escape(dest['url'])}<br>
certification {html.escape(str(run.get('certification_id')))} &middot; started {html.escape(str(run.get('started_at')))}
&middot; finished {html.escape(str(run.get('finished_at')))} &middot; {run.get('duration_s')} s</div>
<table><tr><th>step</th><th>ok</th><th>time (UTC)</th><th>platform answer</th></tr>{''.join(rows)}</table>
</body></html>"""


def screenshot(page: Path, png: Path) -> bool:
    for exe in EDGE:
        if Path(exe).exists() or shutil.which(exe):
            try:
                subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--screenshot={png}",
                                "--window-size=1200,900", page.resolve().as_uri()], timeout=60, capture_output=True)
                return png.exists()
            except (OSError, subprocess.TimeoutExpired):
                continue
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.getenv("API_URL", "http://localhost:8080"))
    args = ap.parse_args()
    headers = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}
    if PACK.exists():
        shutil.rmtree(PACK)
    PACK.mkdir()
    index = ["# Certification pack", "",
             f"Built {time.strftime('%Y-%m-%d %H:%M')} by `python -m tools.make_cert_pack` against {args.api}.", "",
             "Each run publishes a test post, confirms the live URL, reads its status (and metrics where the "
             "connector has them), deletes it and confirms the deletion. Simulated destinations post to the "
             "built-in mock platforms; real local Lemmy and Ghost runs are at the end.", "",
             "| Connector | Destination | Result | Level | Run time | Run record | Screenshot |", "|---|---|---|---|---|---|---|"]
    run_id = uuid.uuid4().hex[:6]
    with httpx.Client(base_url=args.api, headers=headers, timeout=120) as c:
        for ck in CONNECTORS:
            host = f"https://{ck}-pack-{run_id}.mock"
            body: dict[str, Any] = {"url": host, "connector_key": ck, "name": f"Certification pack: {ck}",
                                    "config": config_for(ck, host), "audience_tags": ["ai", "video"], "language": "en"}
            if ck in CREDS:
                body["connection_id"] = c.post("/api/v1/connections",
                                               json={"connector_key": ck, "credentials": CREDS[ck]}).json()["id"]
            dest = c.post("/api/v1/destinations", json=body).json()
            job = c.post(f"/api/v1/destinations/{dest['id']}/certify", json={},
                         headers={"Idempotency-Key": f"pack-{ck}-{run_id}"}).json()
            while (j := c.get(f"/api/v1/jobs/{job['job_id']}").json())["status"] not in ("succeeded", "failed"):
                time.sleep(0.3)
            cert_id = ((j.get("result") or {}).get("certification_id"))
            certs = c.get("/api/v1/certifications", params={"destination_id": dest["id"]}).json()["certifications"]
            record = next((x for x in certs if x.get("id") == cert_id), certs[0] if certs else {})
            # the full stored evidence (it says whether the platform was simulated, and the connector version)
            ev_url = record.get("evidence_url") or (j.get("result") or {}).get("evidence_url")
            ev = c.get(ev_url) if ev_url else None
            run = ev.json() if ev is not None and ev.status_code == 200 else {}
            if not run.get("steps") or "simulated" not in run:
                raise SystemExit(f"{ck}: could not read the stored evidence at {ev_url}; not writing a card without it")
            out = PACK / ck
            out.mkdir()
            (out / "run.json").write_text(json.dumps({"job": j, "certification": record, "run": run}, indent=2,
                                                     ensure_ascii=False, default=str), encoding="utf-8")
            (out / "card.html").write_text(card(ck, dest, run), encoding="utf-8")
            shot = screenshot(out / "card.html", out / "card.png")
            index.append(f"| {ck} | {dest['name']} | {'passed' if run.get('passed') else 'FAILED'} | {run.get('level')} | "
                         f"{run.get('duration_s')} s | [run.json]({ck}/run.json) | "
                         + (f"[card.png]({ck}/card.png)" if shot else f"[card.html]({ck}/card.html)") + " |")
            print(f"{ck}: {'passed' if run.get('passed') else 'FAILED'} {run.get('level')} screenshot={shot}", flush=True)
    index += ["", "## Real local instances (docker compose, 16 Oct 2026)", "",
              "Run on a real Lemmy 0.19.5 and a real Ghost 5 started by `docker compose up`; the posts were deleted "
              "afterwards. Numbers: `outputs/metrics/local_instances.json`.", "",
              "| Destination | Screenshot of the real post | Run record |", "|---|---|---|",
              "| Local Lemmy c/incubrix_test | [local_lemmy_post.jpg](../docs/evidence/local_lemmy_post.jpg) | "
              "[local_instances.json](../outputs/metrics/local_instances.json) |",
              "| Local Ghost blog | [local_ghost_post.jpg](../docs/evidence/local_ghost_post.jpg) | "
              "[local_instances.json](../outputs/metrics/local_instances.json) |"]
    (PACK / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print("wrote", PACK / "INDEX.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
