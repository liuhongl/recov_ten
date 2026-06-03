from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import CallRecordingConfig

LOGGER = logging.getLogger(__name__)

_CUSTOM_EPOCH_MS = 1704067200000
_SEQUENCE_BITS = 12
_WORKER_BITS = 10
_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1
_MAX_WORKER_ID = (1 << _WORKER_BITS) - 1
_WORKER_ID = (hash(socket.gethostname()) ^ os.getpid()) & _MAX_WORKER_ID
_ID_LOCK = threading.Lock()
_LAST_TIMESTAMP = -1
_SEQUENCE = 0


class RecordingUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class OssConfig:
    endpoint: str
    bucket_name: str
    access_key: str
    secret_key: str
    prefix: str = ""
    is_https: bool = False
    region: str = "us-east-1"
    domain: str = ""


@dataclass(frozen=True)
class SysOssRecord:
    oss_id: int
    tenant_id: str
    file_name: str
    original_name: str
    file_suffix: str
    url: str
    ext1: str
    service: str = "minio"


class RecordingUploadStoreProtocol(Protocol):
    async def get_existing_recording_oss_id(
        self,
        context: Mapping[str, Any],
    ) -> int | None: ...

    async def get_active_oss_config(self) -> OssConfig | None: ...

    async def create_sys_oss_record(self, record: SysOssRecord) -> bool: ...

    async def mark_recording_uploaded(
        self,
        context: Mapping[str, Any],
        oss_id: int,
    ) -> bool: ...


class RecordingStorageProtocol(Protocol):
    def upload(
        self,
        oss_config: OssConfig,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> str: ...


class MinioRecordingStorage:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    def upload(
        self,
        oss_config: OssConfig,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        endpoint = _endpoint_host(oss_config.endpoint)
        protocol = "https" if oss_config.is_https else "http"
        region = oss_config.region.strip() or "us-east-1"
        payload_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc)
        date_stamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")

        signed_headers = {
            "content-type": content_type,
            "host": endpoint,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        canonical_headers = "".join(
            f"{key}:{value}\n" for key, value in sorted(signed_headers.items())
        )
        signed_headers_text = ";".join(sorted(signed_headers.keys()))
        canonical_uri = f"/{oss_config.bucket_name}/{quote(object_key, safe='/')}"
        canonical_request = "\n".join(
            [
                "PUT",
                canonical_uri,
                "",
                canonical_headers,
                signed_headers_text,
                payload_hash,
            ]
        )
        credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _signing_key(oss_config.secret_key, date_stamp, region)
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={oss_config.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers_text}, Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Content-Type": content_type,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        put_url = (
            f"{protocol}://{endpoint}/{oss_config.bucket_name}/"
            f"{quote(object_key, safe='/')}"
        )
        request = Request(put_url, data=data, headers=headers, method="PUT")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response.read()
        except HTTPError as err:
            raise RecordingUploadError(f"MinIO upload failed: HTTP {err.code}") from err
        except URLError as err:
            raise RecordingUploadError(f"MinIO upload failed: {err.reason}") from err
        except OSError as err:
            raise RecordingUploadError(f"MinIO upload failed: {err}") from err

        return build_recording_url(oss_config, object_key)


class RecordingUploadService:
    def __init__(
        self,
        store: RecordingUploadStoreProtocol,
        storage: RecordingStorageProtocol,
        config: CallRecordingConfig,
        *,
        id_factory: Callable[[], int] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.storage = storage
        self.config = config
        self.id_factory = id_factory or generate_snowflake_id
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def upload_from_call_result(self, payload: Mapping[str, Any]) -> int | None:
        if not self.config.upload_enabled:
            return None

        context = payload.get("context")
        if not isinstance(context, Mapping):
            context = {}
        call_id = _context_text(context.get("callId"))
        if call_id is None:
            LOGGER.warning("recording_upload_skipped_missing_call_id")
            return None
        existing_oss_id = await self.store.get_existing_recording_oss_id(context)
        if existing_oss_id is not None:
            LOGGER.warning(
                "recording_upload_skipped_already_uploaded "
                "callId=%s existingOssId=%s",
                call_id,
                existing_oss_id,
            )
            return existing_oss_id

        recording_path = _context_text(payload.get("recording_path"))
        if recording_path is None:
            LOGGER.warning("recording_upload_skipped_missing_path callId=%s", call_id)
            return None

        local_path = build_recording_host_path(self.config, recording_path)
        data = await asyncio.to_thread(read_recording_file_when_ready, local_path)

        oss_config = await self.store.get_active_oss_config()
        if oss_config is None:
            raise RecordingUploadError("active sys_oss_config not found")
        validate_oss_config(oss_config)

        tenant_id = _context_text(context.get("tenantId")) or "000000"
        object_key = build_recording_object_key(
            oss_config,
            self.config,
            tenant_id=tenant_id,
            call_id=call_id,
            now=self.now(),
        )
        content_type = "audio/wav"
        url = await asyncio.to_thread(
            self.storage.upload,
            oss_config,
            object_key,
            data,
            content_type,
        )
        oss_id = self.id_factory()
        original_name = f"{call_id}{local_path.suffix.lower() or '.wav'}"
        record = SysOssRecord(
            oss_id=oss_id,
            tenant_id=tenant_id,
            file_name=object_key,
            original_name=original_name,
            file_suffix=Path(original_name).suffix.lower(),
            url=url,
            ext1=json.dumps(
                {
                    "fileSize": len(data),
                    "contentType": content_type,
                    "callId": call_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            service="minio",
        )
        created = await self.store.create_sys_oss_record(record)
        if not created:
            raise RecordingUploadError("sys_oss insert did not create a row")
        marked = await self.store.mark_recording_uploaded(context, oss_id)
        if not marked:
            LOGGER.warning(
                "recording_upload_backfill_noop callId=%s ossId=%s",
                call_id,
                oss_id,
            )
        return oss_id


def build_recording_host_path(
    config: CallRecordingConfig,
    recording_path: str,
) -> Path:
    if not config.host_directory:
        return Path(recording_path)

    recording_dir = config.directory.rstrip("/")
    if recording_path == recording_dir:
        relative_path = ""
    elif recording_path.startswith(f"{recording_dir}/"):
        relative_path = recording_path[len(recording_dir) + 1 :]
    else:
        raise RecordingUploadError(
            "recording path is outside call_recording.directory"
        )
    return Path(config.host_directory) / relative_path


def read_recording_file_when_ready(
    path: Path,
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bytes:
    deadline = monotonic() + timeout_seconds
    last_size: int | None = None
    while True:
        try:
            current_size = path.stat().st_size
        except OSError:
            current_size = 0

        if current_size > 0 and current_size == last_size:
            try:
                return path.read_bytes()
            except OSError as err:
                raise RecordingUploadError(
                    f"recording file is not readable: {path}"
                ) from err

        last_size = current_size if current_size > 0 else None
        if monotonic() >= deadline:
            raise RecordingUploadError(f"recording file is not ready: {path}")
        sleep(poll_interval_seconds)


def build_recording_object_key(
    oss_config: OssConfig,
    config: CallRecordingConfig,
    *,
    tenant_id: str,
    call_id: str,
    now: datetime,
) -> str:
    date_part = now.strftime("%Y%m%d")
    parts = [
        oss_config.prefix.strip("/"),
        config.object_prefix.strip("/"),
        _safe_object_segment(tenant_id, "tenantId"),
        date_part,
        f"{_safe_object_segment(call_id, 'callId')}.wav",
    ]
    return "/".join(part for part in parts if part)


def build_recording_url(oss_config: OssConfig, object_key: str) -> str:
    protocol = "https" if oss_config.is_https else "http"
    domain = oss_config.domain.strip()
    if domain:
        base_url = domain.rstrip("/")
        if "://" not in base_url:
            base_url = f"{protocol}://{base_url}"
        return f"{base_url}/{object_key.lstrip('/')}"
    return (
        f"{protocol}://{_endpoint_host(oss_config.endpoint)}/"
        f"{oss_config.bucket_name}/{object_key}"
    )


def validate_oss_config(oss_config: OssConfig) -> None:
    required_values = {
        "endpoint": oss_config.endpoint,
        "bucket_name": oss_config.bucket_name,
        "access_key": oss_config.access_key,
        "secret_key": oss_config.secret_key,
    }
    for field, value in required_values.items():
        if not value.strip():
            raise RecordingUploadError(f"sys_oss_config.{field} is required")


def generate_snowflake_id() -> int:
    global _LAST_TIMESTAMP, _SEQUENCE

    with _ID_LOCK:
        timestamp = time.time_ns() // 1_000_000
        if timestamp < _LAST_TIMESTAMP:
            timestamp = _LAST_TIMESTAMP
        if timestamp == _LAST_TIMESTAMP:
            _SEQUENCE = (_SEQUENCE + 1) & _MAX_SEQUENCE
            if _SEQUENCE == 0:
                while timestamp <= _LAST_TIMESTAMP:
                    timestamp = time.time_ns() // 1_000_000
        else:
            _SEQUENCE = 0

        _LAST_TIMESTAMP = timestamp
        return (
            ((timestamp - _CUSTOM_EPOCH_MS) << (_WORKER_BITS + _SEQUENCE_BITS))
            | (_WORKER_ID << _SEQUENCE_BITS)
            | _SEQUENCE
        )


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    key = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    for value in (region, "s3", "aws4_request"):
        key = hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()
    return key


def _endpoint_host(endpoint: str) -> str:
    value = endpoint.strip()
    if value.startswith("http://"):
        return value.removeprefix("http://").rstrip("/")
    if value.startswith("https://"):
        return value.removeprefix("https://").rstrip("/")
    return value.rstrip("/")


def _safe_object_segment(value: str, label: str) -> str:
    if not value or any(char in value for char in (" ", "\t", "\n", "\r", "/", "\\")):
        raise RecordingUploadError(f"{label} is not a safe object key segment")
    return value


def _context_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
