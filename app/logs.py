"""Structured JSON logs (Integration Standard point 8) with secret redaction.

Each line carries timestamp, level, job_id and the request's X-Correlation-Id.
Any field whose name looks like a secret is replaced before it is written, and
token-like strings inside messages are masked, so tokens never reach the logs
(must-have 7).
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")
job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")

SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|jwt|credential)", re.I)
# bearer tokens, key=value secrets, long hex/base64 runs and Ghost admin keys (id:secret)
SECRET_VALUE_RES = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|jwt)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    re.compile(r"\b[a-f0-9]{24}:[a-f0-9]{64}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]+"),
]


def redact(value: Any, key: str = "") -> Any:
    if key and SECRET_KEY_RE.search(key) and not key.endswith("_ref"):
        return "***" if value not in (None, "") else value
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    for rx in SECRET_VALUE_RES:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "correlation_id": correlation_id.get() or None,
            "job_id": job_id.get() or None,
        }
        extra = getattr(record, "fields", None)
        if extra:
            for k, v in redact(extra).items():
                payload[k if k not in payload else f"field_{k}"] = v
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


_configured = False


def setup_logging(level: str = "INFO") -> None:
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.handlers = [handler]
        for noisy in ("uvicorn.access", "httpx", "httpcore", "botocore", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True
    root.setLevel(level.upper())


def log(_logger: logging.Logger, _level: int, _message: str, **fields: Any) -> None:
    _logger.log(_level, _message, extra={"fields": fields})
