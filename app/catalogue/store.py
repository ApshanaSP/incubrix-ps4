"""The destination catalogue: one row per destination ("configuration, not code").

Adding forum number 41 is `POST /api/v1/destinations` (or one CSV row) with
connector_key=discourse and config.base_url; no code changes.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app import vault
from app.connectors import registry
from app.connectors.base import Connection, Destination
from app.db import get_db, new_id, now_iso
from app.errors import ApiError

DEST_FIELDS = [
    "name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo", "est_audience",
    "rules_url", "frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed",
    "connector_key", "relevance_score", "relevance_reason", "level", "last_certified_at",
]
TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|fbclid|gclid|ref|ref_src|mc_cid|mc_eid|igshid)$", re.I)


def normalize_url(url: str) -> str:
    """Lower-case scheme and host, drop www., default ports, tracking params,
    fragments and trailing slashes, so the same place is recognised however it
    was typed."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/+", "/", parts.path or "").rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)))
    return urlunsplit((scheme, netloc, path, query, ""))


def to_model(row: dict[str, Any]) -> Destination:
    data = {k: row.get(k) for k in DEST_FIELDS}
    data["id"] = row["id"]
    data["config"] = row.get("config") or {}
    data["audience_tags"] = row.get("audience_tags") or []
    data["disclosure_required"] = bool(row.get("disclosure_required"))
    data["automation_allowed"] = bool(row.get("automation_allowed", 1))
    return Destination(**data)


def public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["disclosure_required"] = bool(out.get("disclosure_required"))
    out["automation_allowed"] = bool(out.get("automation_allowed"))
    return out


def get_destination(dest_id: str) -> dict[str, Any]:
    row = get_db().one("SELECT * FROM destinations WHERE id=?", [dest_id])
    if not row:
        raise ApiError("NOT_FOUND", f"destination {dest_id} not found")
    return row


def list_destinations(**filters: Any) -> list[dict[str, Any]]:
    sql = "SELECT * FROM destinations WHERE 1=1"
    params: list[Any] = []
    for col in ("connector_key", "platform_key", "status", "level", "language"):
        if filters.get(col):
            sql += f" AND {col}=?"
            params.append(filters[col])
    sql += " ORDER BY created_at, name"
    return get_db().all(sql, params)


def upsert_destination(data: dict[str, Any], *, update_existing: bool = True) -> tuple[dict[str, Any], bool]:
    """Insert a catalogue row; if (normalised URL, platform) exists, update it.
    Returns (row, created)."""
    connector_key = data.get("connector_key") or data.get("platform_key")
    if not connector_key:
        raise ApiError("VALIDATION_ERROR", "connector_key is required", details={"field": "connector_key"})
    registry.get(connector_key, check_kill_switch=False)  # unknown connector -> 404 error
    url = data.get("url") or (data.get("config") or {}).get("base_url") or ""
    if not url:
        raise ApiError("VALIDATION_ERROR", "url is required", details={"field": "url"})
    platform = data.get("platform_key") or connector_key
    norm = normalize_url(url)
    # the same forum can host many communities: Lemmy community / Slack channel / feed key make it unique
    cfg = data.get("config") or {}
    for part in ("community", "channel", "feed_key", "publication_id", "category_id"):
        if cfg.get(part) and str(cfg[part]).lower() not in norm.lower():
            norm = f"{norm}#{part}={str(cfg[part]).lower()}"
    db = get_db()
    existing = db.one("SELECT * FROM destinations WHERE normalized_url=? AND platform_key=?", [norm, platform])
    row: dict[str, Any] = {k: data[k] for k in DEST_FIELDS if k in data}
    row.update(platform_key=platform, connector_key=connector_key, url=url, normalized_url=norm,
               config=cfg, updated_at=now_iso())
    if "name" not in row or not row["name"]:
        row["name"] = urlsplit(norm).netloc + (f" {cfg.get('community') or cfg.get('channel') or ''}".rstrip())
    for b in ("disclosure_required", "automation_allowed"):
        if b in row and row[b] is not None:
            row[b] = int(_truthy(row[b]))
    if data.get("connection_id"):
        row["connection_id"] = data["connection_id"]
    if existing:
        if update_existing:
            db.update("destinations", "id", existing["id"], row)
        return get_destination(existing["id"]), False
    row.update(id=data.get("id") or new_id("dst"), created_at=now_iso(), status="uncertified",
               level=data.get("level") or "L0")
    db.insert("destinations", row)
    return get_destination(row["id"]), True


def update_destination(dest_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    get_destination(dest_id)
    allowed = set(DEST_FIELDS) | {"config", "status", "connection_id", "rules_status"}
    clean = {k: v for k, v in changes.items() if k in allowed}
    for b in ("disclosure_required", "automation_allowed"):
        if b in clean:
            clean[b] = int(_truthy(clean[b]))
    clean["updated_at"] = now_iso()
    get_db().update("destinations", "id", dest_id, clean)
    return get_destination(dest_id)


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


# ---------------------------------------------------------------- connections
def create_connection(connector_key: str, credentials: dict[str, Any], name: str | None = None,
                      base_url: str | None = None, expires_at: str | None = None) -> dict[str, Any]:
    registry.get(connector_key, check_kill_switch=False)
    cid = new_id("con")
    get_db().insert("connections", {
        "id": cid, "connector_key": connector_key, "name": name or connector_key,
        "auth_type": registry.get(connector_key, check_kill_switch=False).manifest.get("auth"),
        "credentials_enc": vault.encrypt_credentials({**credentials, **({"base_url": base_url} if base_url else {})}),
        "status": "active", "expires_at": expires_at, "created_at": now_iso(), "updated_at": now_iso(),
    })
    return public_connection(get_connection_row(cid))


def get_connection_row(cid: str) -> dict[str, Any]:
    row = get_db().one("SELECT * FROM connections WHERE id=?", [cid])
    if not row:
        raise ApiError("NOT_FOUND", f"connection {cid} not found")
    return row


def public_connection(row: dict[str, Any]) -> dict[str, Any]:
    creds = vault.decrypt_credentials(row.get("credentials_enc"))
    return {"id": row["id"], "connector_key": row["connector_key"], "name": row["name"], "auth_type": row["auth_type"],
            "status": row["status"], "expires_at": row["expires_at"], "credentials": vault.public_view(creds),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def load_connection(cid: str | None, connector_key: str) -> Connection:
    if not cid:
        return Connection(connector_key=connector_key)  # tiers with auth: none
    row = get_connection_row(cid)
    creds = vault.resolve_refs(vault.decrypt_credentials(row.get("credentials_enc")))
    base_url = creds.pop("base_url", None)
    return Connection(id=row["id"], connector_key=row["connector_key"], credentials=creds, base_url=base_url,
                      status=row["status"], expires_at=row["expires_at"])


def set_connection_status(cid: str, status: str) -> None:
    get_db().update("connections", "id", cid, {"status": status, "updated_at": now_iso()})


def update_connection_credentials(cid: str, credentials: dict[str, Any], expires_at: str | None = None) -> dict[str, Any]:
    row = get_connection_row(cid)
    old = vault.decrypt_credentials(row.get("credentials_enc"))
    merged = {**old, **credentials}
    # a new token has its own expiry (or none); the old token's expiry no longer applies
    get_db().update("connections", "id", cid, {"credentials_enc": vault.encrypt_credentials(merged), "status": "active",
                                                "expires_at": expires_at, "updated_at": now_iso()})
    # destinations paused because of this connection come back
    get_db().execute("UPDATE destinations SET status = CASE WHEN last_certified_at IS NULL THEN 'uncertified' "
                     "ELSE 'certified' END WHERE connection_id=? AND status='paused'", [cid])
    return public_connection(get_connection_row(cid))
