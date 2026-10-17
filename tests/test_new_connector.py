"""The connector template: a new connector made with tools/new_connector.py certifies and passes its tests at once."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_new_connector_from_the_template_passes_its_tests(tmp_path):
    for part in ("app", "tests", "tools", "batch_inputs", "catalogue"):
        shutil.copytree(ROOT / part, tmp_path / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("pytest.ini", "openapi.yaml"):
        shutil.copy(ROOT / f, tmp_path / f)
    made = subprocess.run([sys.executable, "-m", "tools.new_connector", "circle", "--name", "Circle", "--root", str(tmp_path)],
                          cwd=tmp_path, capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    again = subprocess.run([sys.executable, "-m", "tools.new_connector", "circle", "--root", str(tmp_path)],
                           cwd=tmp_path, capture_output=True, text=True)
    assert again.returncode == 1  # never overwrites
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_connector_circle.py"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    assert run.stdout.split("[")[0].count(".") == 6  # six tests, all passed (pytest.ini keeps the output short)
