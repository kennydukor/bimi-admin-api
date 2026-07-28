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
        self._client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )

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


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        if settings.aws_access_key_id or settings.s3_endpoint_url:
            _storage = S3Storage()
        else:
            _storage = LocalStorage()
    return _storage
