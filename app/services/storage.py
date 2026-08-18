"""
Object storage for the recycle bin.

Thin wrapper over S3 (boto3). Deleted rows are written here as CSV plus a small
JSON sidecar describing the columns and their types, so a restore can round-trip
values back into Postgres with the right types even years later.

A LocalStorage backend (writes under ./_recycle_bin_local) is provided so the
portal runs end-to-end in dev without AWS credentials. Both implement the same
interface; `get_storage()` picks one from settings.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Protocol

from app.core.config import settings


class ObjectStorage(Protocol):
    def put_text(self, key: str, body: str, content_type: str) -> None: ...
    def get_text(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...
    def presigned_url(self, key: str, filename: str) -> str: ...


class S3Storage:
    """Production backend — Amazon S3 (or any S3-compatible endpoint)."""

    def __init__(self) -> None:
        import boto3  # imported lazily so dev without boto3 still starts

        self._bucket = settings.s3_bucket

        # Build kwargs conditionally. For REAL AWS S3 you must NOT pass
        # endpoint_url — boto3 derives it from the region. Passing an empty
        # string raises "Invalid endpoint: ". Only set endpoint_url for an
        # S3-compatible service like MinIO, where it's a real URL. Likewise,
        # omit credentials when unset so boto3 can fall back to its default
        # chain (IAM role, env, shared config).
        def _clean(val):
            return val if (val is not None and str(val).strip() != "") else None

        kwargs = {"region_name": _clean(settings.s3_region)}
        endpoint = _clean(settings.s3_endpoint_url)
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        access_key = _clean(settings.aws_access_key_id)
        secret_key = _clean(settings.aws_secret_access_key)
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        self._client = boto3.client("s3", **kwargs)

    def put_text(self, key: str, body: str, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType=content_type,
        )

    def get_text(self, key: str) -> str:
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        return obj["Body"].read().decode("utf-8")

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def presigned_url(self, key: str, filename: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=900,
        )


class LocalStorage:
    """Dev backend — writes to the local filesystem, no AWS needed."""

    def __init__(self, root: str = "_recycle_bin_local") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = self._root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_text(self, key: str, body: str, content_type: str) -> None:
        self._path(key).write_text(body, encoding="utf-8")

    def get_text(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presigned_url(self, key: str, filename: str) -> str:
        # No signing locally; the download endpoint streams the bytes instead.
        return f"/api/v1/admin/recycle/local-download?key={key}&filename={filename}"


_storage: ObjectStorage | None = None


def _is_set(val) -> bool:
    return val is not None and str(val).strip() != ""


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        # Use S3 when real credentials OR a custom endpoint are configured;
        # treat empty-string env vars as unset so a blank value doesn't
        # half-enable S3 and crash boto3.
        if _is_set(settings.aws_access_key_id) or _is_set(settings.s3_endpoint_url):
            _storage = S3Storage()
        else:
            _storage = LocalStorage()
    return _storage
