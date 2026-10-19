"""All settings come from environment variables (Integration Standard point 6).

Every setting is listed in .env.example. Nothing here holds a path or key that
cannot be overridden.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # .env is optional; real deployments use the environment directly
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# Use the operating system's certificate store (for machines whose antivirus or proxy
# re-signs HTTPS). Optional: needs the `truststore` package.
if os.getenv("USE_SYSTEM_CERTS", "").lower() in ("1", "true", "yes"):
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:  # pragma: no cover
        pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    # service
    service_name: str = field(default_factory=lambda: os.getenv("SERVICE_NAME", "incubrix-longtail"))
    # on Render the platform sets RENDER_EXTERNAL_URL, so links work with no extra setting
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL") or os.getenv(
        "RENDER_EXTERNAL_URL") or "http://localhost:8080")
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", ""))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    # access control for the live demo (empty = open, for local use)
    api_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", ""))
    rate_limit_per_minute: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MINUTE", 120))
    # requests with a wrong API token per minute, counted for the whole service (stops token guessing)
    failed_auth_per_minute: int = field(default_factory=lambda: _int("FAILED_AUTH_PER_MINUTE", 30))
    # secrets: key used to encrypt stored connection credentials
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    # simulation: all platform calls go to the built-in mock platforms
    simulation_mode: bool = field(default_factory=lambda: _bool("SIMULATION_MODE", True))
    # where mock platforms live when not in-process (for example the compose "mocks" service)
    mock_base_url: str = field(default_factory=lambda: os.getenv("MOCK_BASE_URL", ""))
    # outbound HTTP
    http_timeout_s: float = field(default_factory=lambda: _float("HTTP_TIMEOUT_S", 15.0))
    max_backoff_s: float = field(default_factory=lambda: _float("MAX_BACKOFF_S", 60.0))
    unavailable_trip_after: int = field(default_factory=lambda: _int("UNAVAILABLE_TRIP_AFTER", 5))
    # after this long with the breaker open, one trial call is let through; success closes it again
    breaker_cooldown_s: float = field(default_factory=lambda: _float("BREAKER_COOLDOWN_S", 300.0))
    user_agent: str = field(
        default_factory=lambda: os.getenv("USER_AGENT", "IncuBrixLongTail/1.0 (+https://incubrix.com)")
    )
    # storage: S3-compatible when S3_ENDPOINT is set, otherwise the local data dir
    s3_endpoint: str = field(default_factory=lambda: os.getenv("S3_ENDPOINT", ""))
    s3_public_endpoint: str = field(default_factory=lambda: os.getenv("S3_PUBLIC_ENDPOINT", ""))
    s3_bucket: str = field(default_factory=lambda: os.getenv("S3_BUCKET", "incubrix-longtail"))
    s3_access_key: str = field(default_factory=lambda: os.getenv("S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: os.getenv("S3_SECRET_KEY", ""))
    s3_region: str = field(default_factory=lambda: os.getenv("S3_REGION", "us-east-1"))
    link_ttl_s: int = field(default_factory=lambda: _int("LINK_TTL_S", 7 * 24 * 3600))
    # jobs
    job_workers: int = field(default_factory=lambda: _int("JOB_WORKERS", 4))
    callback_secret: str = field(default_factory=lambda: os.getenv("CALLBACK_SECRET", ""))
    # AI provider adapters: mock | groq | gemini
    ai_provider: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", "mock"))
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-latest"))
    # the batch runner's inputs, also served read-only to the dashboard's hard-case tab
    batch_inputs_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "BATCH_INPUTS_DIR", str(Path(__file__).resolve().parent.parent / "batch_inputs"))))
    # catalogue import
    reachability_mode: str = field(default_factory=lambda: os.getenv("REACHABILITY_MODE", "online"))
    reachability_concurrency: int = field(default_factory=lambda: _int("REACHABILITY_CONCURRENCY", 50))
    reachability_timeout_s: float = field(default_factory=lambda: _float("REACHABILITY_TIMEOUT_S", 6.0))
    # certification and recertification
    cleanup_deadline_s: int = field(default_factory=lambda: _int("CLEANUP_DEADLINE_S", 600))
    recert_after_days: int = field(default_factory=lambda: _int("RECERT_AFTER_DAYS", 25))
    recert_interval_s: int = field(default_factory=lambda: _int("RECERT_INTERVAL_S", 3600))
    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", True))
    alert_webhook_url: str = field(default_factory=lambda: os.getenv("ALERT_WEBHOOK_URL", ""))
    # audience used for relevance scoring
    audience_tags: list[str] = field(
        default_factory=lambda: [
            t.strip()
            for t in os.getenv("AUDIENCE_TAGS", "ai,video,marketing,saas,creators,founders").split(",")
            if t.strip()
        ]
    )

    def resolved_db_path(self) -> str:
        if self.db_path:
            return self.db_path
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return str(self.data_dir / "longtail.sqlite3")


_settings = Settings()


def get_settings() -> Settings:
    return _settings


def reload_settings() -> Settings:
    """Re-read the environment (used by tests)."""
    global _settings
    _settings = Settings()
    return _settings
