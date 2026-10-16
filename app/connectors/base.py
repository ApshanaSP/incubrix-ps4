"""The IncuBrix connector contract (Appendix A).

Every connector subclasses `Connector`, ships one manifest YAML file and
implements the contract methods. Methods return results only and raise
`ConnectorError` with a shared code; they never write to the service database.
"""
from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.connectors.http import PlatformHTTP
from app.errors import ConnectorError

MANIFEST_DIR = Path(__file__).parent / "manifests"
_call_notes: ContextVar[list[str] | None] = ContextVar("connector_call_notes", default=None)


def note(message: str) -> None:
    """Record something a connector did on its own (for example a re-login), so the caller can
    report it as a warning instead of it happening silently."""
    notes = _call_notes.get()
    if notes is not None:
        notes.append(message)


def collect_notes() -> list[str]:
    """Start collecting notes for the current call; returns the list that note() appends to."""
    notes: list[str] = []
    _call_notes.set(notes)
    return notes


class Media(BaseModel):
    url: str
    type: Literal["image", "video", "audio", "file"] = "image"
    mime: str | None = None
    alt: str | None = None
    title: str | None = None
    duration_s: float | None = None
    licence: str | None = None


class Rendition(BaseModel):
    """One piece of content, already shaped for one destination."""

    model_config = ConfigDict(extra="allow")

    title: str = ""
    body: str = ""
    body_format: Literal["markdown", "html", "text"] = "markdown"
    link: str | None = None
    canonical_url: str | None = None
    media: list[Media] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    promotional: bool | None = None
    ai_generated: bool = False
    disclosure: str | None = None
    language: str | None = None
    surface: str | None = None
    content_id: str | None = None


class Destination(BaseModel):
    """A catalogue row (see the destination record in Appendix A) plus `config`,
    the instance parameters for its connector (base_url, community, channel...)."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    name: str = ""
    url: str = ""
    platform_key: str = ""
    connector_key: str = ""
    surface: str | None = None
    category: str | None = None
    audience_tags: list[str] = Field(default_factory=list)
    language: str | None = None
    geo: str | None = None
    est_audience: int | None = None
    rules_url: str | None = None
    frequency_cap: str | None = None
    self_promo_allowed: str | None = None
    link_policy: str | None = None
    disclosure_required: bool = False
    automation_allowed: bool = True
    relevance_score: int | None = None
    relevance_reason: str | None = None
    level: str | None = None
    last_certified_at: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class Connection(BaseModel):
    """An authorised account. `credentials` are resolved secrets, held in memory
    only for the duration of a call."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    connector_key: str | None = None
    credentials: dict[str, Any] = Field(default_factory=dict)
    base_url: str | None = None
    status: str = "active"
    expires_at: str | None = None


def load_manifest(key: str) -> dict[str, Any]:
    with open(MANIFEST_DIR / f"{key}.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def fingerprint(title: str, body: str) -> str:
    norm = re.sub(r"\s+", " ", f"{title}\n{body[:400]}").strip().lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:20]


def encode_post_id(native_id: Any, base_url: str | None) -> str:
    """platform_post_id carries the instance so get_status/delete can find it
    with only (platform_post_id, connection), as the contract requires."""
    return f"{native_id}|{base_url}" if base_url else str(native_id)


def decode_post_id(platform_post_id: str, connection: Connection | None = None) -> tuple[str, str | None]:
    if "|" in platform_post_id:
        native, base = platform_post_id.split("|", 1)
        return native, base
    return platform_post_id, (connection.base_url if connection else None)


URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


class Connector:
    key: str = ""

    def __init__(self) -> None:
        self.manifest = load_manifest(self.key)

    # ---------------------------------------------------------------- helpers
    @property
    def capabilities(self) -> list[str]:
        return list(self.manifest.get("capabilities", []))

    def surface_spec(self, destination: Destination, surface: str | None = None) -> dict[str, Any]:
        surfaces = self.manifest.get("surfaces", [])
        wanted = surface or destination.surface
        chosen = next((s for s in surfaces if s["key"] == wanted), surfaces[0] if surfaces else {})
        spec = dict(chosen.get("spec", {}))
        spec.update(destination.config.get("spec", {}))  # per-destination overrides, e.g. a stricter forum
        spec["content_types"] = destination.config.get("content_types", chosen.get("content_types", []))
        spec["surface"] = chosen.get("key")
        return spec

    def base_url(self, destination: Destination | None, connection: Connection | None) -> str:
        url = (destination.config.get("base_url") if destination else None) or (connection.base_url if connection else None)
        if not url:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE", f"{self.key}: no base_url for this destination", retryable=False,
                details={"cause": "MISSING_INSTANCE_PARAM", "param": "base_url"},
            )
        return url.rstrip("/")

    def http(self, scope: str) -> PlatformHTTP:
        return PlatformHTTP(
            self.key,
            scope,
            error_map=self.manifest.get("error_map"),
            rate_limits=(self.manifest.get("rate_limits") or {}).get("per_connection"),
        )

    @staticmethod
    def credential(connection: Connection, name: str) -> str:
        value = connection.credentials.get(name)
        if not value:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED", f"connection has no '{name}'; reconnect the account",
                details={"missing_credential": name},
            )
        return str(value)

    def check_auth_status(self, connection: Connection) -> None:
        if connection.status in ("revoked", "expired"):
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED", f"connection is {connection.status}; the owner must reconnect",
                details={"connection_status": connection.status},
            )

    # ---------------------------------------------------------------- contract
    async def validate(self, rendition: Rendition, destination: Destination, connection: Connection | None) -> dict[str, Any]:
        violations = spec_violations(rendition, self.surface_spec(destination, rendition.surface))
        return {"ok": not violations, "violations": violations}

    async def publish(
        self, rendition: Rendition, destination: Destination, connection: Connection, idempotency_key: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_status(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        raise NotImplementedError

    async def delete(self, platform_post_id: str, connection: Connection) -> dict[str, Any]:
        raise NotImplementedError

    async def fetch_metrics(self, platform_post_id: str, connection: Connection, since: str | None) -> dict[str, Any]:
        raise NotImplementedError

    async def refresh_auth(self, connection: Connection) -> dict[str, Any]:
        raise NotImplementedError

    async def build_assist_pack(self, rendition: Rendition, destination: Destination) -> dict[str, Any]:
        raise NotImplementedError

    # ---------------------------------------------------------------- not in the contract
    async def find_existing(
        self, rendition: Rendition, destination: Destination, connection: Connection, idempotency_key: str
    ) -> dict[str, Any] | None:
        """After a timeout the post may exist even though we never saw the
        answer. Look it up so a retry returns it instead of posting twice.
        Returns None if the connector cannot tell (the caller then refuses to
        post blind)."""
        return None

    async def certify_probe(self, destination: Destination, connection: Connection) -> dict[str, Any]:
        """Cheap read-only check that the instance answers with the fields we rely on."""
        return {"ok": True}

    def implements(self, method: str) -> bool:
        base = getattr(Connector, method, None)
        mine = getattr(type(self), method, None)
        return mine is not None and mine is not base


def normalise_number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def empty_metrics() -> dict[str, int]:
    return {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0, "watch_time_s": 0, "clicks": 0}


def spec_violations(rendition: Rendition, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Format limits. Each violation names the field, the limit and the actual value."""
    out: list[dict[str, Any]] = []

    def add(field: str, message: str, **extra: Any) -> None:
        out.append({"code": "DEST_SPEC_VIOLATION", "rule": "format", "field": field, "action": "block",
                    "message": message, **extra})

    title_max = spec.get("title_max_chars")
    if spec.get("title_required") and not rendition.title.strip():
        add("title", "a title is required")
    if title_max and len(rendition.title) > title_max:
        add("title", f"title is {len(rendition.title)} characters; the limit is {title_max}",
            limit=title_max, actual=len(rendition.title))
    title_min = spec.get("title_min_chars")
    if title_min and rendition.title and len(rendition.title.strip()) < title_min:
        add("title", f"title is shorter than {title_min} characters", limit=title_min, actual=len(rendition.title))
    body_max = spec.get("body_max_chars")
    if body_max and len(rendition.body) > body_max:
        add("body", f"body is {len(rendition.body)} characters; the limit is {body_max}",
            limit=body_max, actual=len(rendition.body))
    body_min = spec.get("body_min_chars")
    if body_min and len(rendition.body.strip()) < body_min:
        add("body", f"body is shorter than {body_min} characters", limit=body_min, actual=len(rendition.body))
    if spec.get("links_allowed") is False:
        links = URL_RE.findall(rendition.body) + ([rendition.link] if rendition.link else [])
        if links:
            add("body", "this destination does not allow links", links=links[:5])
    allowed = set(spec.get("content_types") or [])
    if allowed:
        for i, m in enumerate(rendition.media):
            kind = m.type
            if kind not in allowed and not (kind == "video" and "video_link" in allowed):
                add(f"media[{i}]", f"{kind} media is not accepted here (accepted: {', '.join(sorted(allowed))})",
                    media_url=m.url)
        mimes = spec.get("media_mime_types")
        if mimes:
            for i, m in enumerate(rendition.media):
                if m.mime and m.mime not in mimes:
                    add(f"media[{i}]", f"media type {m.mime} is not accepted (accepted: {', '.join(mimes)})")
    max_media = spec.get("max_media")
    if max_media is not None and len(rendition.media) > max_media:
        add("media", f"{len(rendition.media)} media items; the limit is {max_media}", limit=max_media)
    max_tags = spec.get("max_tags")
    if max_tags is not None and len(rendition.tags) > max_tags:
        add("tags", f"{len(rendition.tags)} tags; the limit is {max_tags}", limit=max_tags, actual=len(rendition.tags))
    tag_pattern = spec.get("tag_pattern")
    if tag_pattern:
        bad = [t for t in rendition.tags if not re.fullmatch(tag_pattern, t)]
        if bad:
            add("tags", f"tags {bad} do not match the destination's tag format {tag_pattern}")
    return out
