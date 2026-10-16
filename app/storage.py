"""Files out (Integration Standard point 7).

Outputs go to S3-compatible storage (MinIO in the compose file) and come back
as presigned download links. Without S3_ENDPOINT, files are written under
DATA_DIR/files and served by the API through HMAC-signed links, so the service
also runs with no external storage.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

from app.config import get_settings


class Storage:
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def link(self, key: str) -> str:
        raise NotImplementedError

    def put_json(self, key: str, obj: Any) -> str:
        data = json.dumps(obj, indent=2, ensure_ascii=False, default=str).encode()
        return self.put_bytes(key, data, "application/json")

    def put_text(self, key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> str:
        return self.put_bytes(key, text.encode(), content_type)


class LocalStorage(Storage):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("bad storage key")
        return p

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return self.link(key)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def link(self, key: str) -> str:
        s = get_settings()
        expires = int(time.time()) + s.link_ttl_s
        sig = sign_link(key, expires)
        return f"{s.public_base_url.rstrip('/')}/api/v1/files/{key}?expires={expires}&sig={sig}"


def sign_link(key: str, expires: int) -> str:
    secret = (get_settings().secret_key or "local-dev-only-secret-change-me").encode()
    return hmac.new(secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_link(key: str, expires: int, sig: str) -> bool:
    return expires >= time.time() and hmac.compare_digest(sign_link(key, expires), sig)


class S3Storage(Storage):
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        s = get_settings()
        self.bucket = s.s3_bucket
        common = dict(
            aws_access_key_id=s.s3_access_key or None,
            aws_secret_access_key=s.s3_secret_key or None,
            region_name=s.s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self.client = boto3.client("s3", endpoint_url=s.s3_endpoint, **common)
        # presigned links must use the host the downloader can reach
        self.public = boto3.client("s3", endpoint_url=s.s3_public_endpoint or s.s3_endpoint, **common)
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=ctype)
        return self.link(key)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def link(self, key: str) -> str:
        return self.public.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=get_settings().link_ttl_s
        )


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        _storage = S3Storage() if s.s3_endpoint else LocalStorage(s.data_dir / "files")
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
