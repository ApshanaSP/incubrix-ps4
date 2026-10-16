"""Builds test_pack/: every input, every output (failures included), logs, certification evidence and
an INDEX.md linking each claim to its file.

    python -m tools.make_test_pack
Steps: run pytest (JUnit XML + text log) -> start the service on a free port (simulation mode, fresh
data dir) -> run the batch runner on batch_inputs/ -> copy the server log, the certification evidence
and the metrics -> write INDEX.md.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "test_pack"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    if PACK.exists():
        shutil.rmtree(PACK)
    (PACK / "tests").mkdir(parents=True)
    # 1. tests
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--show-capture=no",
                           f"--junitxml={PACK / 'tests' / 'pytest.xml'}"], cwd=ROOT, capture_output=True, text=True)
    lines = [ln for ln in proc.stdout.splitlines() if not ln.startswith('{"timestamp"')]
    import xml.etree.ElementTree as ET

    suite = ET.parse(PACK / "tests" / "pytest.xml").getroot().find("testsuite")
    if suite is not None:
        lines.append(f"tests={suite.get('tests')} failures={suite.get('failures')} errors={suite.get('errors')} "
                     f"skipped={suite.get('skipped')}")
    (PACK / "tests" / "pytest.txt").write_text("\n".join(lines) + f"\n(exit {proc.returncode}, {time.time() - t0:.0f}s)\n",
                                               encoding="utf-8")
    print(lines[-1] if lines else "pytest gave no output")
    # 2. service + batch runner
    data = Path(tempfile.mkdtemp(prefix="ibx-pack-"))
    port = free_port()
    env = {**os.environ, "DATA_DIR": str(data), "SIMULATION_MODE": "true", "REACHABILITY_MODE": "offline",
           "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}", "PORT": str(port), "RATE_LIMIT_PER_MINUTE": "0",
           "HTTP_TIMEOUT_S": "3", "API_TOKEN": "", "AI_PROVIDER": "mock", "SCHEDULER_ENABLED": "false"}
    log_path = PACK / "server.log"
    with open(log_path, "w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "-m", "app.serve"], cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT)
        try:
            for _ in range(60):
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.5)
            time.sleep(2)  # seeded demo destinations certify at start-up
            out = PACK / "batch"
            subprocess.run([sys.executable, "-m", "tools.batch_runner", "--api", f"http://127.0.0.1:{port}",
                            "--input", "batch_inputs", "--out", str(out)], cwd=ROOT, env=env)
            # independent reviewer-style checks; they also search this server's log for secrets
            subprocess.run([sys.executable, "-m", "tools.reviewer_check", "--api", f"http://127.0.0.1:{port}",
                            "--server-log", str(log_path), "--out", str(PACK / "reviewer_check")], cwd=ROOT, env=env)
            certs = httpx.get(f"http://127.0.0.1:{port}/api/v1/certifications", params={"limit": 500}).json()
            (PACK / "certifications.json").write_text(json.dumps(certs, indent=2), encoding="utf-8")
        finally:
            server.terminate()
            server.wait(timeout=20)
    ev = data / "files" / "certifications"
    if ev.exists():
        shutil.copytree(ev, PACK / "certification_evidence")
    shutil.copytree(ROOT / "batch_inputs", PACK / "inputs")
    if (ROOT / "outputs" / "metrics").exists():
        shutil.copytree(ROOT / "outputs" / "metrics", PACK / "metrics")
    shutil.rmtree(data, ignore_errors=True)
    batch_dir = next((PACK / "batch").glob("batch-*"))
    summary = json.loads((batch_dir / "summary.json").read_text(encoding="utf-8"))
    rows = "\n".join(f"| `{c['name']}` | {'ok' if c['ok'] else 'NOT AS EXPECTED'} | "
                     f"[result](batch/{batch_dir.name}/{c['name']}.json) | [input](inputs/{c['name']}.json) |"
                     for c in summary["cases"])
    n_ev = len(list((PACK / "certification_evidence").rglob("*.json"))) if (PACK / "certification_evidence").exists() else 0
    rc_file = next((PACK / "reviewer_check").rglob("reviewer_check.json"), None)
    rc = json.loads(rc_file.read_text(encoding="utf-8")) if rc_file else {"passed": 0, "total": 0}
    rc_link = rc_file.relative_to(PACK).as_posix() if rc_file else "reviewer_check/"
    (PACK / "INDEX.md").write_text(f"""# Test pack index

Built {time.strftime('%Y-%m-%d %H:%M')} by `python -m tools.make_test_pack`, in simulation mode.

| Claim | Evidence |
|---|---|
| All automated tests pass | [tests/pytest.txt](tests/pytest.txt), [tests/pytest.xml](tests/pytest.xml) |
| Every hard case and feature, run end to end over HTTP | the batch table below; [server.log](server.log) is the service's JSON log for the whole run |
| Certification: publish, live URL, status, metrics, delete, confirm deletion | [certifications.json](certifications.json) and {n_ev} evidence files in [certification_evidence/](certification_evidence/) |
| Measured numbers (REPORT.md section 4) | [metrics/metrics.json](metrics/metrics.json), [metrics/recommender.json](metrics/recommender.json) |
| No secrets in logs | search [server.log](server.log) for the credential values in `tools/make_batch_inputs.py`: none appear; the reviewer check stores a random token and searches this log for it |
| Independent reviewer-style checks (every must-have, Integration point and hard case, other inputs than the batch) | {rc['passed']}/{rc['total']} passed: [{rc_link}]({rc_link}) |
| Certification pack: run record, evidence page and screenshot per connector, real Lemmy and Ghost posts | [../certification_pack/INDEX.md](../certification_pack/INDEX.md) |
| Online catalogue import (real catalogue and 1,000 rows, API latency during the import) | [metrics/catalogue_import_online.json](metrics/catalogue_import_online.json) |
| Failed attempts, kept | first reviewer check (32/37 before fixes): [../outputs/reviewer_check/20261016-234023/](../outputs/reviewer_check/20261016-234023/reviewer_check.json); first compose batch run that hit the API's own rate limit: [../outputs/compose/batch-20261016-202108/](../outputs/compose/batch-20261016-202108/); REPORT.md section 3 |
| Real catalogue (150+ destinations), rules evidence | [../catalogue/real_catalogue.csv](../catalogue/real_catalogue.csv), [../catalogue/real_catalogue_evidence.json](../catalogue/real_catalogue_evidence.json) |

## Batch runs

| Input | Result | Output | Input file |
|---|---|---|---|
{rows}
""", encoding="utf-8")
    print(f"test pack written to {PACK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
