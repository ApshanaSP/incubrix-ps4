"""Output manifest attached to every result (Integration Standard point 9):
models/providers and versions, settings, seed, timings, cost estimate,
warnings and licence/rights information."""
from __future__ import annotations

import platform
import time
from typing import Any

from app.config import get_settings
from app.connectors import registry

SERVICE_VERSION = "1.0.0"

RIGHTS = {
    "content": "Content is supplied by the IncuBrix creator who requested distribution; IncuBrix posts it under "
               "that creator's licence. No third-party media is added by this service.",
    "ai_generated_output": "AI-written text (rules proposals, relevance reasons) is marked with ai_generated=true.",
    "service_licence": "Service code and its default-path dependencies are licensed for commercial use "
                       "(see SOURCES.md); no AGPL code is shipped.",
}


class Timer:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        self.marks[name] = round(time.monotonic() - self.started, 3)

    def as_dict(self) -> dict[str, float]:
        return {**self.marks, "total_s": round(time.monotonic() - self.started, 3)}


def build(kind: str, *, timer: Timer | None = None, connectors: list[str] | None = None,
          ai_usage: list[dict[str, Any]] | None = None, warnings: list[str] | None = None,
          settings_used: dict[str, Any] | None = None, seed: int | None = 0, ai_generated: bool = False,
          units: int | None = None) -> dict[str, Any]:
    s = get_settings()
    ai_usage = [u for u in (ai_usage or []) if u]
    cost = round(sum(u.get("cost_usd", 0.0) for u in ai_usage), 6)
    conns = []
    for key in connectors or []:
        try:
            m = registry.get(key, check_kill_switch=False).manifest
            conns.append({"key": key, "version": m.get("version"), "tier": m.get("tier")})
        except Exception:  # noqa: BLE001
            conns.append({"key": key})
    return {
        "manifest_version": "1.0",
        "kind": kind,
        "service": {"name": s.service_name, "version": SERVICE_VERSION, "python": platform.python_version()},
        "connectors": conns,
        "models": ai_usage or [{"provider": "none", "note": "no AI model used for this result"}],
        "settings": {"simulation_mode": s.simulation_mode, "ai_provider": s.ai_provider, **(settings_used or {})},
        "seed": seed,
        "timings": timer.as_dict() if timer else {},
        "cost_estimate_usd": {"total": cost, "per_unit": round(cost / units, 6) if units else None, "units": units},
        "warnings": warnings or [],
        "ai_generated": ai_generated,
        "rights": RIGHTS,
    }
