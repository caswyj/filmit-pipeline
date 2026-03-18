from __future__ import annotations

import mimetypes
from pathlib import Path

import boto3
from botocore.client import Config

from app.core.config import settings


class ObjectStorageService:
    def __init__(self) -> None:
        self._bucket = (settings.tos_bucket or "").strip()
        self._region = (settings.tos_region or "").strip()
        self._endpoint = (settings.tos_endpoint or "").strip()
        self._ak = (settings.tos_access_key_id or "").strip()
        self._sk = (settings.tos_secret_access_key or "").strip()
        self._session_token = (settings.tos_session_token or "").strip()
        self._presign_expire_sec = int(settings.tos_presign_expire_sec or 3600)
        self._client = None

    def is_configured(self) -> bool:
        return bool(self._bucket and self._region and self._endpoint and self._ak and self._sk)

    def upload_local_file(self, local_path: str | Path, object_key: str) -> str:
        if not self.is_configured():
            raise ValueError("TOS is not configured")
        path = Path(local_path).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"local file not found: {path}")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client = self._ensure_client()
        with path.open("rb") as fh:
            client.put_object(Bucket=self._bucket, Key=object_key, Body=fh, ContentType=content_type)
        return self.presign_get_url(object_key)

    def presign_get_url(self, object_key: str, expires_in: int | None = None) -> str:
        if not self.is_configured():
            raise ValueError("TOS is not configured")
        client = self._ensure_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": object_key},
            ExpiresIn=int(expires_in or self._presign_expire_sec),
        )

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        session = boto3.session.Session()
        self._client = session.client(
            "s3",
            region_name=self._region,
            endpoint_url=self._endpoint if self._endpoint.startswith("http") else f"https://{self._endpoint}",
            aws_access_key_id=self._ak,
            aws_secret_access_key=self._sk,
            aws_session_token=self._session_token or None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        return self._client
