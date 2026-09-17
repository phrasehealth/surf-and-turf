"""Report persistence: local disk (dev) or S3 with presigned URLs (prod)."""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import settings


@dataclass(frozen=True)
class StoredReport:
    report_id: str
    filename: str
    url: str
    size_bytes: int
    created_at: str


def _slug(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return (s or "report")[:60]


def _make_name(title: str) -> tuple[str, str]:
    rid = uuid.uuid4().hex[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return rid, f"{stamp}-{_slug(title)}-{rid}.pdf"


class LocalStorage:
    def __init__(self, directory: Path, base_url: str):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url

    async def save(self, title: str, pdf: bytes, conversation_id: str) -> StoredReport:
        rid, name = _make_name(title)
        (self.dir / name).write_bytes(pdf)
        return StoredReport(
            report_id=rid, filename=name, url=f"{self.base_url}/reports/{name}",
            size_bytes=len(pdf), created_at=datetime.now(timezone.utc).isoformat(),
        )

    def path_for(self, filename: str) -> Path | None:
        if "/" in filename or ".." in filename or not filename.endswith(".pdf"):
            return None
        p = self.dir / filename
        return p if p.is_file() else None


class S3Storage:
    def __init__(self, bucket: str, prefix: str, ttl_s: int):
        import boto3

        self.s3 = boto3.client("s3", region_name=settings.aws_region)
        self.bucket, self.prefix, self.ttl = bucket, prefix, ttl_s

    def _put(self, key: str, pdf: bytes, conversation_id: str) -> str:
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=pdf, ContentType="application/pdf",
            Metadata={"conversation-id": conversation_id},
            ServerSideEncryption="AES256",
        )
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{key.rsplit("/", 1)[-1]}"'},
            ExpiresIn=self.ttl,
        )

    async def save(self, title: str, pdf: bytes, conversation_id: str) -> StoredReport:
        rid, name = _make_name(title)
        key = f"{self.prefix}{conversation_id}/{name}"
        url = await asyncio.to_thread(self._put, key, pdf, conversation_id)
        return StoredReport(
            report_id=rid, filename=name, url=url, size_bytes=len(pdf),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def path_for(self, filename: str):  # local downloads are not served in S3 mode
        return None


def get_storage():
    if settings.report_storage == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("REPORT_STORAGE=s3 requires REPORT_S3_BUCKET")
        return S3Storage(settings.s3_bucket, settings.s3_prefix, settings.presign_ttl_s)
    return LocalStorage(settings.report_dir, settings.public_base_url)


storage = get_storage()
