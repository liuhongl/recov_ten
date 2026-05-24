from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from urllib.error import HTTPError

from app.flow_callback import (
    FlowCallbackEvent,
    HttpFlowCallbackWriter,
    LoggingFlowCallbackWriter,
    build_flow_callback_http_request,
    build_flow_callback_event,
)


def test_build_flow_callback_event_uses_task_id_and_business_id():
    event = build_flow_callback_event(
        {
            "tenantId": "000000",
            "taskId": "2050000000000100001",
            "businessId": "biz-call-1",
        },
        status="SUCCESS",
        message="外呼完成，转写已写入",
    )

    assert event is not None
    assert event.tenant_id == "000000"
    assert event.task_id == "2050000000000100001"
    assert event.business_id == "biz-call-1"
    assert event.status == "SUCCESS"
    assert event.message == "外呼完成，转写已写入"
    assert isinstance(event.timestamp, int)
    assert event.to_message() == {
        "tenantId": "000000",
        "taskId": "2050000000000100001",
        "businessId": "biz-call-1",
        "status": "SUCCESS",
        "message": "外呼完成，转写已写入",
        "timestamp": event.timestamp,
    }


def test_build_flow_callback_event_skips_missing_task_id():
    event = build_flow_callback_event(
        {"tenantId": "000000"},
        status="SUCCESS",
        message="外呼完成，转写已写入",
    )

    assert event is None


def test_logging_flow_callback_writer_logs_without_sending(caplog):
    writer = LoggingFlowCallbackWriter(topic="recov-flow-callback")
    event = FlowCallbackEvent(
        tenant_id="000000",
        task_id="task-1",
        business_id="biz-1",
        status="FAILED",
        message="无人接听",
        timestamp=1770000000000,
    )

    with caplog.at_level(logging.INFO):
        assert writer.publish(event) is True

    assert "flow_callback_publish" in caplog.text
    assert "topic=recov-flow-callback" in caplog.text
    assert "taskId=task-1" in caplog.text
    assert "status=FAILED" in caplog.text


def test_build_flow_callback_http_request_signs_raw_body():
    event = FlowCallbackEvent(
        tenant_id="100001",
        task_id="task-1",
        business_id="call-1",
        status="SUCCESS",
        message="AI外呼完成，通话转写已生成",
        timestamp=1770000010000,
    )

    request = build_flow_callback_http_request(
        event,
        base_url="https://flow.example",
        path="/system/recov/flow/external/callback",
        client_id="python-ai-call",
        secret="secret",
        timestamp_seconds=1770000001,
        nonce="nonce-1",
    )

    body_hash = hashlib.sha256(request.raw_body).hexdigest()
    canonical = "\n".join(
        [
            "POST",
            "/system/recov/flow/external/callback",
            "1770000001",
            "nonce-1",
            body_hash,
        ]
    )
    expected_signature = base64.b64encode(
        hmac.new(b"secret", canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    assert request.url == "https://flow.example/system/recov/flow/external/callback"
    assert json.loads(request.raw_body.decode("utf-8")) == event.to_message()
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-LC-Client-Id"] == "python-ai-call"
    assert request.headers["X-LC-Timestamp"] == "1770000001"
    assert request.headers["X-LC-Nonce"] == "nonce-1"
    assert request.headers["X-LC-Signature"] == expected_signature
    assert (
        request.headers["X-LC-Signature-Path"]
        == "/system/recov/flow/external/callback"
    )


def test_http_flow_callback_writer_posts_signed_body():
    requests = []

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"code":200,"msg":"ok"}'

    def fake_opener(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    writer = HttpFlowCallbackWriter(
        base_url="https://flow.example",
        path="/system/recov/flow/external/callback",
        client_id="python-ai-call",
        secret="secret",
        timeout_seconds=3.5,
        opener=fake_opener,
        nonce_factory=lambda: "nonce-1",
        timestamp_factory=lambda: 1770000001,
    )
    event = FlowCallbackEvent(
        tenant_id="100001",
        task_id="task-1",
        business_id="call-1",
        status="FAILED",
        message="AI外呼未接听，未产生有效通话内容",
        timestamp=1770000010000,
    )

    assert writer.publish(event) is True

    request, timeout = requests[0]
    assert timeout == 3.5
    assert request.full_url == "https://flow.example/system/recov/flow/external/callback"
    assert request.get_method() == "POST"
    assert json.loads(request.data.decode("utf-8")) == event.to_message()
    assert request.headers["X-lc-client-id"] == "python-ai-call"
    assert request.headers["X-lc-signature-path"] == (
        "/system/recov/flow/external/callback"
    )


def test_http_flow_callback_writer_retries_temporary_http_error():
    attempts = 0

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"code":200}'

    def fake_opener(request, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                hdrs=None,
                fp=None,
            )
        return FakeResponse()

    writer = HttpFlowCallbackWriter(
        base_url="https://flow.example",
        path="/system/recov/flow/external/callback",
        client_id="python-ai-call",
        secret="secret",
        max_attempts=2,
        retry_backoff_seconds=0,
        opener=fake_opener,
    )

    assert writer.publish(
        FlowCallbackEvent(
            tenant_id="100001",
            task_id="task-1",
            business_id="call-1",
            status="FAILED",
            message="外呼失败",
            timestamp=1770000010000,
        )
    )
    assert attempts == 2
