from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.config import CallRecordingConfig
from app.recording_upload import (
    OssConfig,
    RecordingUploadError,
    RecordingUploadService,
    build_recording_host_path,
    build_recording_object_key,
    build_recording_url,
    read_recording_file_when_ready,
    validate_oss_config,
)


def test_build_recording_host_path_maps_container_recording_to_host_mount(tmp_path):
    config = CallRecordingConfig(
        enabled=True,
        directory="/var/lib/freeswitch/recordings",
        upload_enabled=True,
        host_directory=str(tmp_path),
    )

    path = build_recording_host_path(
        config,
        "/var/lib/freeswitch/recordings/990000000000032001.wav",
    )

    assert path == tmp_path / "990000000000032001.wav"


def test_build_recording_object_key_uses_oss_prefix_tenant_date_and_call_id():
    oss_config = OssConfig(
        endpoint="minio.example:9000",
        bucket_name="recov",
        access_key="access",
        secret_key="secret",
        prefix="business",
        is_https=False,
        region="us-east-1",
    )
    recording_config = CallRecordingConfig(
        enabled=True,
        upload_enabled=True,
        object_prefix="recordings",
    )

    key = build_recording_object_key(
        oss_config,
        recording_config,
        tenant_id="000000",
        call_id="990000000000032001",
        now=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    assert key == "business/recordings/000000/20260603/990000000000032001.wav"


def test_build_recording_url_uses_domain_with_explicit_scheme_and_base_path():
    oss_config = OssConfig(
        endpoint="minio.example:9000",
        bucket_name="recov",
        access_key="access",
        secret_key="secret",
        prefix="business",
        is_https=False,
        region="us-east-1",
        domain="https://cdn.example.com/assets/",
    )

    url = build_recording_url(
        oss_config,
        "business/recordings/000000/20260603/990000000000032001.wav",
    )

    assert (
        url
        == "https://cdn.example.com/assets/business/recordings/000000/"
        "20260603/990000000000032001.wav"
    )


def test_build_recording_url_adds_scheme_for_domain_without_protocol():
    oss_config = OssConfig(
        endpoint="minio.example:9000",
        bucket_name="recov",
        access_key="access",
        secret_key="secret",
        is_https=True,
        domain="cdn.example.com",
    )

    url = build_recording_url(
        oss_config,
        "recordings/000000/20260603/990000000000032001.wav",
    )

    assert (
        url
        == "https://cdn.example.com/recordings/000000/20260603/"
        "990000000000032001.wav"
    )


def test_validate_oss_config_rejects_missing_required_fields():
    oss_config = OssConfig(
        endpoint="minio.example:9000",
        bucket_name="",
        access_key="access",
        secret_key="secret",
    )

    with pytest.raises(RecordingUploadError) as exc_info:
        validate_oss_config(oss_config)

    assert "sys_oss_config.bucket_name is required" in str(exc_info.value)


def test_read_recording_file_when_ready_waits_for_stable_non_empty_file(tmp_path):
    recording_file = tmp_path / "990000000000032001.wav"
    recording_file.write_bytes(b"partial")
    sleeps = []

    def sleep_without_waiting(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            recording_file.write_bytes(b"complete-wav")

    data = read_recording_file_when_ready(
        recording_file,
        timeout_seconds=1.0,
        poll_interval_seconds=0.01,
        sleep=sleep_without_waiting,
    )

    assert data == b"complete-wav"
    assert sleeps == [0.01, 0.01]


def test_read_recording_file_when_ready_rejects_empty_file_after_timeout(tmp_path):
    recording_file = tmp_path / "990000000000032001.wav"
    recording_file.write_bytes(b"")
    now = 0.0

    def monotonic():
        return now

    def sleep_without_waiting(seconds):
        nonlocal now
        now += seconds

    with pytest.raises(RecordingUploadError) as exc_info:
        read_recording_file_when_ready(
            recording_file,
            timeout_seconds=0.02,
            poll_interval_seconds=0.01,
            sleep=sleep_without_waiting,
            monotonic=monotonic,
        )

    assert "recording file is not ready" in str(exc_info.value)


def test_recording_upload_service_uploads_wav_writes_sys_oss_and_backfills_call_record(
    tmp_path,
):
    recording_file = tmp_path / "990000000000032001.wav"
    recording_file.write_bytes(b"RIFF-wav-bytes")

    class Store:
        def __init__(self):
            self.created_records = []
            self.marked_uploads = []

        async def get_existing_recording_oss_id(self, context):
            return None

        async def get_active_oss_config(self):
            return OssConfig(
                endpoint="minio.example:9000",
                bucket_name="recov",
                access_key="access",
                secret_key="secret",
                prefix="business",
                is_https=False,
                region="us-east-1",
            )

        async def create_sys_oss_record(self, record):
            self.created_records.append(record)
            return True

        async def mark_recording_uploaded(self, context, oss_id):
            self.marked_uploads.append((context, oss_id))
            return True

    class Storage:
        def __init__(self):
            self.uploads = []

        def upload(self, oss_config, object_key, data, content_type):
            self.uploads.append((oss_config, object_key, data, content_type))
            return f"http://minio.example:9000/recov/{object_key}"

    store = Store()
    storage = Storage()
    service = RecordingUploadService(
        store,
        storage,
        CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            upload_enabled=True,
            host_directory=str(tmp_path),
            object_prefix="recordings",
        ),
        id_factory=lambda: 123456789,
        now=lambda: datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    oss_id = asyncio.run(
        service.upload_from_call_result(
            {
                "call_id": "media-call-id",
                "recording_path": (
                    "/var/lib/freeswitch/recordings/990000000000032001.wav"
                ),
                "context": {
                    "tenantId": "000000",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
    )

    assert oss_id == 123456789
    assert len(storage.uploads) == 1
    _, object_key, data, content_type = storage.uploads[0]
    assert object_key == "business/recordings/000000/20260603/990000000000032001.wav"
    assert data == b"RIFF-wav-bytes"
    assert content_type == "audio/wav"
    assert len(store.created_records) == 1
    record = store.created_records[0]
    assert record.oss_id == 123456789
    assert record.tenant_id == "000000"
    assert record.file_name == object_key
    assert record.original_name == "990000000000032001.wav"
    assert record.file_suffix == ".wav"
    assert record.url == f"http://minio.example:9000/recov/{object_key}"
    assert record.service == "minio"
    assert json.loads(record.ext1) == {
        "fileSize": len(b"RIFF-wav-bytes"),
        "contentType": "audio/wav",
        "callId": "990000000000032001",
    }
    assert store.marked_uploads == [
        (
            {
                "tenantId": "000000",
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            },
            123456789,
        )
    ]


def test_recording_upload_service_skips_upload_when_recording_already_uploaded(
    tmp_path,
):
    recording_file = tmp_path / "990000000000032001.wav"
    recording_file.write_bytes(b"RIFF-wav-bytes")

    class Store:
        def __init__(self):
            self.checked_contexts = []

        async def get_existing_recording_oss_id(self, context):
            self.checked_contexts.append(context)
            return 987654321

        async def get_active_oss_config(self):
            raise AssertionError("existing recording must skip oss config lookup")

        async def create_sys_oss_record(self, record):
            raise AssertionError("existing recording must skip sys_oss insert")

        async def mark_recording_uploaded(self, context, oss_id):
            raise AssertionError("existing recording must skip backfill update")

    class Storage:
        def upload(self, oss_config, object_key, data, content_type):
            raise AssertionError("existing recording must skip MinIO upload")

    store = Store()
    service = RecordingUploadService(
        store,
        Storage(),
        CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            upload_enabled=True,
            host_directory=str(tmp_path),
            object_prefix="recordings",
        ),
    )

    oss_id = asyncio.run(
        service.upload_from_call_result(
            {
                "call_id": "media-call-id",
                "recording_path": (
                    "/var/lib/freeswitch/recordings/990000000000032001.wav"
                ),
                "context": {
                    "tenantId": "000000",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
    )

    assert oss_id == 987654321
    assert store.checked_contexts == [
        {
            "tenantId": "000000",
            "callId": "990000000000032001",
            "debtId": "2049810626160668673",
        }
    ]
