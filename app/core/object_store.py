"""Object storage for run artifacts (owner: DATA, ADR 0018).

Backends store opaque bytes under validated keys. Artifact keys are content
addressed and tenant prefixed (``tenants/<tenant>/sha256/<aa>/<digest>``) so a
tenant can never address another tenant's object and identical uploads dedupe.
The local filesystem backend is the dev/test default; the S3-compatible backend
signs requests with AWS SigV4 over ``httpx`` and adds no required dependency.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import os
import re
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from app.core.config import Settings, settings

KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]*(/[A-Za-z0-9][A-Za-z0-9._=-]*)*$")
TENANT_SEGMENT = re.compile(r"[^a-z0-9-]")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_KEY_LENGTH = 512


class ObjectStoreError(RuntimeError):
    """The backend failed; callers must treat the write/read as not having happened."""


class ObjectNotFoundError(ObjectStoreError):
    pass


def validate_key(key: str) -> str:
    if (
        not key
        or len(key) > MAX_KEY_LENGTH
        or not KEY_PATTERN.match(key)
        or any(part in {".", ".."} for part in key.split("/"))
    ):
        raise ValueError(f"Invalid object key: {key!r}")
    return key


def tenant_prefix(tenant_uuid: object | None, tenant_id: str) -> str:
    """Stable per-tenant key prefix; legacy dev tenants without a UUID get a sanitized alias."""
    if tenant_uuid is not None:
        return f"tenants/{uuid.UUID(str(tenant_uuid))}"
    alias = TENANT_SEGMENT.sub("-", tenant_id.lower()).strip("-")[:64] or "unknown"
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]
    return f"tenants/legacy-{alias}-{digest}"


def content_key(tenant_uuid: object | None, tenant_id: str, sha256_hex: str) -> str:
    if not SHA256_HEX.match(sha256_hex):
        raise ValueError("sha256 digest must be 64 lowercase hex characters")
    return validate_key(
        f"{tenant_prefix(tenant_uuid, tenant_id)}/sha256/{sha256_hex[:2]}/{sha256_hex}"
    )


@runtime_checkable
class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> None:
        """Store ``data`` at ``key`` atomically (readers see old or new bytes, never partial)."""
        ...

    async def get(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        """Return bytes ``[start, end]`` (inclusive end, ``None`` = to EOF)."""
        ...

    async def size(self, key: str) -> int | None:
        """Object length in bytes, or ``None`` when absent."""
        ...

    async def delete(self, key: str) -> bool:
        """Remove the object; return whether it existed. Idempotent."""
        ...


class LocalFilesystemObjectStore:
    """Filesystem backend: atomic temp-file + rename writes, path traversal refused."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if self.root not in path.parents:
            raise ValueError(f"Object key escapes the store root: {key!r}")
        return path

    def _put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _get(self, key: str, start: int, end: int | None) -> bytes:
        path = self._path(key)
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                if end is None:
                    return handle.read()
                return handle.read(max(end - start + 1, 0))
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    def _size(self, key: str) -> int | None:
        try:
            return self._path(key).stat().st_size
        except FileNotFoundError:
            return None

    def _delete(self, key: str) -> bool:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        for parent in path.parents:
            if parent == self.root or self.root not in parent.parents:
                break
            try:
                parent.rmdir()
            except OSError:
                break
        return True

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        return await asyncio.to_thread(self._get, key, start, end)

    async def size(self, key: str) -> int | None:
        return await asyncio.to_thread(self._size, key)

    async def delete(self, key: str) -> bool:
        return await asyncio.to_thread(self._delete, key)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    url: httpx.URL,
    headers: dict[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: dt.datetime,
    service: str = "s3",
) -> dict[str, str]:
    """Return ``headers`` plus ``Authorization``/``x-amz-*`` for an AWS SigV4 request."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    signed = {k.lower(): v.strip() for k, v in headers.items()}
    signed["host"] = url.netloc.decode("ascii")
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash
    names = sorted(signed)
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(url.params.multi_items())
    )
    canonical = "\n".join(
        [
            method,
            quote(url.path or "/", safe="/-_.~"),
            canonical_query,
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical.encode("utf-8"))]
    )
    key = _hmac(f"AWS4{secret_key}".encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = dict(headers)
    result["x-amz-date"] = amz_date
    result["x-amz-content-sha256"] = payload_hash
    result["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return result


class S3ObjectStore:
    """S3-compatible backend (AWS, MinIO, Ceph) with path-style addressing and SigV4."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self._transport = transport
        self._timeout = timeout

    def _url(self, key: str) -> httpx.URL:
        return httpx.URL(f"{self.endpoint}/{self.bucket}/{validate_key(key)}")

    async def _request(
        self,
        method: str,
        key: str,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = self._url(key)
        signed = sigv4_headers(
            method=method,
            url=url,
            headers=headers or {},
            payload_hash=_sha256(content),
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            now=dt.datetime.now(dt.UTC),
        )
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                return await client.request(method, url, content=content, headers=signed)
        except httpx.HTTPError as exc:
            raise ObjectStoreError(f"{method} {key} failed: {exc}") from exc

    async def put(self, key: str, data: bytes) -> None:
        response = await self._request(
            "PUT",
            key,
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        if response.status_code not in (200, 201, 204):
            raise ObjectStoreError(f"PUT {key} returned {response.status_code}")

    async def get(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        headers = {}
        if start or end is not None:
            headers["range"] = f"bytes={start}-{'' if end is None else end}"
        response = await self._request("GET", key, headers=headers)
        if response.status_code == 404:
            raise ObjectNotFoundError(key)
        if response.status_code == 416:
            return b""
        if response.status_code not in (200, 206):
            raise ObjectStoreError(f"GET {key} returned {response.status_code}")
        return response.content

    async def size(self, key: str) -> int | None:
        response = await self._request("HEAD", key)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ObjectStoreError(f"HEAD {key} returned {response.status_code}")
        return int(response.headers.get("content-length", "0"))

    async def delete(self, key: str) -> bool:
        existed = await self.size(key) is not None
        response = await self._request("DELETE", key)
        if response.status_code not in (200, 204, 404):
            raise ObjectStoreError(f"DELETE {key} returned {response.status_code}")
        return existed


def build_object_store(config: Settings = settings) -> ObjectStore:
    if config.artifact_store_backend == "s3":
        missing = [
            name
            for name, value in (
                ("ARTIFACT_S3_ENDPOINT", config.artifact_s3_endpoint),
                ("ARTIFACT_S3_BUCKET", config.artifact_s3_bucket),
                ("ARTIFACT_S3_ACCESS_KEY", config.artifact_s3_access_key),
                ("ARTIFACT_S3_SECRET_KEY", config.artifact_s3_secret_key),
            )
            if not value
        ]
        if missing:
            raise ValueError("S3 artifact store requires " + ", ".join(missing))
        assert config.artifact_s3_endpoint and config.artifact_s3_bucket
        assert config.artifact_s3_access_key and config.artifact_s3_secret_key
        return S3ObjectStore(
            endpoint=config.artifact_s3_endpoint,
            bucket=config.artifact_s3_bucket,
            access_key=config.artifact_s3_access_key,
            secret_key=config.artifact_s3_secret_key,
            region=config.artifact_s3_region,
        )
    return LocalFilesystemObjectStore(config.artifact_store_dir)
