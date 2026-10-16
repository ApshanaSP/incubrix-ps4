"""Connector registry and kill switch."""
from __future__ import annotations

from typing import Any

from app.connectors.assisted import AssistedConnector
from app.connectors.base import Connector
from app.connectors.devto import DevtoConnector
from app.connectors.discourse import DiscourseConnector
from app.connectors.ghost import GhostConnector
from app.connectors.hashnode import HashnodeConnector
from app.connectors.lemmy import LemmyConnector
from app.connectors.mrss import MrssConnector
from app.connectors.slack import SlackConnector
from app.connectors.webhook import WebhookConnector
from app.db import get_db
from app.errors import ApiError, ConnectorError

CORE = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"]
ADVANCED = ["hashnode", "slack", "mrss"]

_CLASSES: dict[str, type[Connector]] = {
    c.key: c
    for c in (DiscourseConnector, LemmyConnector, DevtoConnector, GhostConnector, WebhookConnector, AssistedConnector,
              HashnodeConnector, SlackConnector, MrssConnector)
}
_INSTANCES: dict[str, Connector] = {}


def keys() -> list[str]:
    return list(_CLASSES)


def get(key: str, *, check_kill_switch: bool = True) -> Connector:
    if key not in _CLASSES:
        raise ApiError("UNKNOWN_CONNECTOR", f"no connector '{key}'", details={"known": keys()})
    if check_kill_switch:
        row = get_db().one("SELECT disabled, reason FROM kill_switch WHERE connector_key=?", [key])
        if row and row["disabled"]:
            raise ConnectorError("CONNECTOR_DISABLED", f"connector '{key}' is switched off: {row['reason'] or 'no reason'}")
    if key not in _INSTANCES:
        _INSTANCES[key] = _CLASSES[key]()
    return _INSTANCES[key]


def manifests() -> list[dict[str, Any]]:
    out = []
    for key in keys():
        m = dict(get(key, check_kill_switch=False).manifest)
        row = get_db().one("SELECT disabled, reason FROM kill_switch WHERE connector_key=?", [key])
        m["disabled"] = bool(row and row["disabled"])
        m["group"] = "core" if key in CORE else "advanced"
        out.append(m)
    return out


def set_kill_switch(key: str, disabled: bool, reason: str | None) -> None:
    get(key, check_kill_switch=False)
    get_db().execute(
        "INSERT INTO kill_switch(connector_key, disabled, reason) VALUES(?,?,?) "
        "ON CONFLICT(connector_key) DO UPDATE SET disabled=excluded.disabled, reason=excluded.reason",
        [key, int(disabled), reason],
    )
