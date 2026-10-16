"""Batch runner (Integration Standard point 10).

Runs every test input in a folder (or one file) against the API and writes all
results to one output folder.

Each input is a JSON file:
  {"name": "...", "steps": [
      {"method": "POST", "path": "/api/v1/publications", "body": {...},
       "headers": {"Idempotency-Key": "..."}, "wait": true, "save_as": "first"},
      ...]}
A step may refer to an earlier step's result with "{{first.result.results.0.post_id}}", and to
"{{run}}", a short id unique to this run (so every run creates its own destinations).
A step with "upload": "files/x.csv" sends that file (relative to the input folder) as multipart
field "file", with "form" as the other fields.
A .jsonl file holds one input per line. Files starting with "_" are skipped.

Usage:
  python -m tools.batch_runner --api http://localhost:8080 --input batch_inputs --out outputs
  (API_TOKEN from the environment is sent as a bearer token)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

PLACEHOLDER = re.compile(r"\{\{([^}]+)\}\}")


def lookup(ctx: dict[str, Any], dotted: str) -> Any:
    cur: Any = ctx
    for part in dotted.strip().split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def fill(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full = PLACEHOLDER.fullmatch(value)
        if full:
            return lookup(ctx, full.group(1))
        return PLACEHOLDER.sub(lambda m: str(lookup(ctx, m.group(1))), value)
    if isinstance(value, list):
        return [fill(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    return value


def load_inputs(path: Path) -> list[tuple[str, dict[str, Any]]]:
    files = sorted(p for p in path.iterdir() if p.suffix in (".json", ".jsonl") and not p.name.startswith("_")) \
        if path.is_dir() else [path]
    out = []
    for f in files:
        if f.suffix == ".jsonl":
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
                if line.strip():
                    item = json.loads(line)
                    out.append((item.get("name") or f"{f.stem}_{i + 1}", item))
        else:
            item = json.loads(f.read_text(encoding="utf-8"))
            out.append((item.get("name") or f.stem, item))
    return out


def send(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One request; waits and repeats when the service's own rate limit answers 429 SERVICE_RATE_LIMIT
    (that request was refused before any work, so repeating it is safe)."""
    for _ in range(10):
        resp = client.request(method, path, **kwargs)
        if resp.status_code != 429:
            return resp
        try:
            err = resp.json()["error"]
        except (ValueError, KeyError, TypeError):
            return resp
        if err.get("code") != "SERVICE_RATE_LIMIT":
            return resp  # a platform 429 is a result the input may expect
        time.sleep(float((err.get("details") or {}).get("retry_after_s") or 5))
    return resp


def wait_job(client: httpx.Client, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            job = send(client, "GET", f"/api/v1/jobs/{job_id}").json()
        except httpx.TransportError:  # a dropped keep-alive connection; reading a job is safe to repeat
            time.sleep(0.5)
            continue
        if job.get("status") in ("succeeded", "failed"):
            return job
        time.sleep(0.25)
    return {"job_id": job_id, "status": "timeout"}


def run_case(client: httpx.Client, name: str, case: dict[str, Any], timeout: float,
             base_dir: Path | None = None) -> dict[str, Any]:
    import uuid

    ctx: dict[str, Any] = {"run": uuid.uuid4().hex[:8]}
    steps_out = []
    started = time.time()
    ok = True
    for i, step in enumerate(case.get("steps", [case])):
        try:
            step = fill(step, ctx)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # an earlier step did not return what this one needs: record it and stop this input
            steps_out.append({"step": i + 1, "error": f"cannot fill placeholders from earlier steps: {exc!r}"})
            ok = False
            break
        if step.get("sleep"):
            time.sleep(float(step["sleep"]))
            continue
        method = step.get("method", "POST").upper()
        t0 = time.time()
        if step.get("upload"):
            fpath = (base_dir or Path(".")) / step["upload"]
            with open(fpath, "rb") as fh:
                resp = send(client, method, step["path"], files={"file": (fpath.name, fh.read())},
                                      data={k: str(v) for k, v in (step.get("form") or {}).items()},
                                      headers=step.get("headers") or {})
        else:
            resp = send(client, method, step["path"], json=step.get("body"), params=step.get("params"),
                                  headers=step.get("headers") or {})
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        record: dict[str, Any] = {"step": i + 1, "method": method, "path": step["path"], "http_status": resp.status_code,
                                  "correlation_id": resp.headers.get("x-correlation-id"), "response": body}
        if step.get("wait", True) and resp.status_code == 202 and isinstance(body, dict) and body.get("job_id"):
            job = wait_job(client, body["job_id"], timeout)
            record["job"] = job
            body = job
        record["seconds"] = round(time.time() - t0, 3)
        expect = step.get("expect_status")
        if expect and resp.status_code != expect:
            ok = False
            record["unexpected_status"] = True
        for dotted, wanted in (step.get("expect") or {}).items():
            try:
                got = lookup({"body": body}, "body." + dotted)
            except (KeyError, IndexError, TypeError, ValueError):
                got = "<missing>"
            check = {"field": dotted, "expected": wanted, "got": got, "ok": got == wanted}
            record.setdefault("checks", []).append(check)
            ok = ok and check["ok"]
        steps_out.append(record)
        if step.get("save_as"):
            ctx[step["save_as"]] = body
    return {"name": name, "description": case.get("description"), "ok": ok, "seconds": round(time.time() - started, 3),
            "steps": steps_out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.getenv("API_URL", "http://localhost:8080"))
    ap.add_argument("--input", default="batch_inputs", help="folder of .json/.jsonl inputs, or one file")
    ap.add_argument("--out", default="outputs", help="results go to <out>/batch-<timestamp>/")
    ap.add_argument("--timeout", type=float, default=300, help="seconds to wait for each job")
    args = ap.parse_args(argv)

    headers = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}
    out_dir = Path(args.out) / f"batch-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = load_inputs(Path(args.input))
    summary = []
    with httpx.Client(base_url=args.api, headers=headers, timeout=60) as client:
        for name, case in cases:
            base = Path(args.input) if Path(args.input).is_dir() else Path(args.input).parent
            result = run_case(client, name, case, args.timeout, base)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            (out_dir / f"{safe}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            last = result["steps"][-1] if result["steps"] else {}
            summary.append({"name": name, "ok": result["ok"], "seconds": result["seconds"],
                            "last_http_status": last.get("http_status"),
                            "last_job_status": (last.get("job") or {}).get("status")})
            print(f"{'OK ' if result['ok'] else 'ERR'} {name} ({result['seconds']}s)")
    (out_dir / "summary.json").write_text(json.dumps({"api": args.api, "cases": summary}, indent=2), encoding="utf-8")
    print(f"\n{sum(s['ok'] for s in summary)}/{len(summary)} inputs ran as expected. Results: {out_dir}")
    return 0 if all(s["ok"] for s in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
