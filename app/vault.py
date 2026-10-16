"""Connection credentials: encrypted at rest, never logged, never returned.

A connection's credentials can be given inline (stored encrypted with
SECRET_KEY) or as references such as {"api_key_ref": "env:DISCOURSE_API_KEY"},
which are read from the environment only when a connector needs them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().secret_key or "local-dev-only-secret-change-me"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_credentials(creds: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(creds).encode()).decode()


def decrypt_credentials(blob: str | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode()))
    except InvalidToken as exc:  # SECRET_KEY changed
        raise ValueError("stored credentials cannot be decrypted with the current SECRET_KEY") from exc


def resolve_refs(creds: dict[str, Any]) -> dict[str, Any]:
    """Turn {"x_ref": "env:NAME"} into {"x": os.environ["NAME"]}."""
    out: dict[str, Any] = {}
    for k, v in creds.items():
        if k.endswith("_ref") and isinstance(v, str) and v.startswith("env:"):
            out[k[: -len("_ref")]] = os.getenv(v[4:], "")
        else:
            out[k] = v
    return out


def public_view(creds: dict[str, Any]) -> dict[str, Any]:
    """What the API may show about credentials: which fields exist, never values."""
    return {k: ("<ref>" if k.endswith("_ref") else "<set>") for k in creds}
