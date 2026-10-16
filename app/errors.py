"""Error codes and the JSON error shape (Integration Standard point 5).

Connector methods raise only the shared contract codes from Appendix A. The
service adds a few codes of its own for its HTTP API. All are listed in the
README.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# code -> (http status, retryable, meaning)
SHARED_CODES: dict[str, tuple[int, bool | str, str]] = {
    "DEST_SPEC_VIOLATION": (422, False, "Content breaks the destination's format limits."),
    "DEST_RULE_FREQUENCY_CAP": (409, "later", "Frequency cap reached; reschedule after the cap window."),
    "CONNECTION_AUTH_EXPIRED": (401, "after_reconnect", "Token expired or revoked; ask the owner to reconnect."),
    "PLATFORM_RATE_LIMIT": (429, True, "Platform rate limit; back off using its reset time."),
    "PLATFORM_QUOTA_EXHAUSTED": (429, "next_window", "Daily quota used up; reschedule."),
    "PLATFORM_CONTENT_REJECTED": (422, False, "Platform refused the content."),
    "PLATFORM_UNAVAILABLE": (502, True, "Platform unavailable; retry, stop after 5 consecutive failures."),
    "CONNECTOR_DISABLED": (503, False, "Kill switch is on for this connector."),
}

SERVICE_CODES: dict[str, tuple[int, bool | str, str]] = {
    "VALIDATION_ERROR": (400, False, "The request body or parameters are invalid."),
    "UNAUTHORIZED": (401, False, "Missing or wrong API token."),
    "NOT_FOUND": (404, False, "The resource does not exist."),
    "IDEMPOTENCY_KEY_CONFLICT": (409, False, "This Idempotency-Key was used with a different request body."),
    "DEST_RULE_VIOLATION": (409, False, "The post breaks a posting rule (self-promotion, links, disclosure, automation)."),
    "DEST_NOT_CERTIFIED": (409, False, "The destination is not certified (or was demoted) and is excluded."),
    "DEST_RULES_UNCONFIRMED": (409, False, "AI-proposed rules are waiting for a person to confirm them."),
    "UNSUPPORTED_METHOD": (400, False, "The connector does not implement this method."),
    "UNKNOWN_CONNECTOR": (404, False, "No connector with this key."),
    "UPSTREAM_FETCH_FAILED": (502, True, "A URL given as input could not be fetched."),
    "AI_PROVIDER_FAILED": (502, True, "The configured AI provider failed; the mock adapter is used as fallback only if enabled."),
    "SERVICE_RATE_LIMIT": (429, True, "Too many requests to this service."),
    "SIMULATION_ONLY": (403, False, "This endpoint is available only in simulation mode."),
    "INTERNAL_ERROR": (500, True, "Unexpected error; see logs with the correlation id."),
}

ALL_CODES = {**SHARED_CODES, **SERVICE_CODES}


@dataclass
class ApiError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None
    retryable: bool | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")
        status, retry, _ = ALL_CODES.get(self.code, (500, False, ""))
        if self.http_status is None:
            self.http_status = status
        if self.retryable is None:
            self.retryable = retry is True or retry in ("later", "next_window")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": bool(self.retryable),
                "details": self.details or {},
            }
        }


class ConnectorError(ApiError):
    """Raised by connector methods; code must be one of SHARED_CODES."""

    def __post_init__(self) -> None:
        if self.code not in SHARED_CODES:
            raise ValueError(f"connector raised non-contract code {self.code}")
        super().__post_init__()
