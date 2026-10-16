"""Writes openapi.yaml from the running app definition, so the file always matches the service.

    python -m tools.export_openapi
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

os.environ.setdefault("SCHEDULER_ENABLED", "false")
from app.main import app  # noqa: E402

out = Path(__file__).resolve().parent.parent / "openapi.yaml"
spec = app.openapi()
out.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(f"wrote {out} (OpenAPI {spec['openapi']}, {len(spec['paths'])} paths)")
