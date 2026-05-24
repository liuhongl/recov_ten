from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

LOGGER = logging.getLogger(__name__)

FLOW_CALLBACK_STATUSES = {
    "ACCEPTED",
    "PROGRESS",
    "SUCCESS",
    "FAILED",
    "SKIPPED",
}


@dataclass(frozen=True)
class FlowCallbackEvent:
    tenant_id: str
    task_id: str
    business_id: str
    status: str
    message: str
    timestamp: int

    def to_message(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "taskId": self.task_id,
            "businessId": self.business_id,
            "status": self.status,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class FlowCallbackHttpRequest:
    url: str
    headers: dict[str, str]
    raw_body: bytes


class FlowCallbackWriterProtocol(Protocol):
    def publish(self, event: FlowCallbackEvent) -> bool: ...


class LoggingFlowCallbackWriter:
    def __init__(self, *, topic: str) -> None:
        self.topic = topic

    def publish(self, event: FlowCallbackEvent) -> bool:
        LOGGER.info(
            "flow_callback_publish topic=%s tag=%s tenantId=%s taskId=%s "
            "businessId=%s status=%s message=%s timestamp=%s",
            self.topic,
            event.status,
            event.tenant_id,
            event.task_id,
            event.business_id,
            event.status,
            event.message,
            event.timestamp,
        )
        return True


class HttpFlowCallbackWriter:
    def __init__(
        self,
        *,
        base_url: str,
        path: str,
        client_id: str,
        secret: str,
        timeout_seconds: float = 10.0,
        max_attempts: int = 1,
        retry_backoff_seconds: float = 0.2,
        opener=None,
        nonce_factory=None,
        timestamp_factory=None,
    ) -> None:
        self.base_url = base_url
        self.path = path
        self.client_id = client_id
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._opener = opener or _urlopen
        self._nonce_factory = nonce_factory or (lambda: uuid.uuid4().hex)
        self._timestamp_factory = timestamp_factory or (lambda: int(time.time()))

    def publish(self, event: FlowCallbackEvent) -> bool:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            request_data = build_flow_callback_http_request(
                event,
                base_url=self.base_url,
                path=self.path,
                client_id=self.client_id,
                secret=self.secret,
                timestamp_seconds=int(self._timestamp_factory()),
                nonce=str(self._nonce_factory()),
            )
            request = Request(
                request_data.url,
                data=request_data.raw_body,
                headers=request_data.headers,
                method="POST",
            )
            try:
                response = self._opener(request, timeout=self.timeout_seconds)
                status = _response_status(response)
                body = response.read()
                if status >= 500:
                    raise FlowCallbackTemporaryError(
                        f"flow callback temporary failed: {status} {body!r}"
                    )
                if status >= 400:
                    raise FlowCallbackRejectedError(
                        f"flow callback rejected: {status} {body!r}"
                    )
                _raise_for_business_error(body)
                LOGGER.info(
                    "flow_callback_http_published url=%s tenantId=%s taskId=%s "
                    "businessId=%s status=%s",
                    request_data.url,
                    event.tenant_id,
                    event.task_id,
                    event.business_id,
                    event.status,
                )
                return True
            except HTTPError as err:
                body = err.read() if err.fp is not None else b""
                last_error = err
                if err.code < 500:
                    raise FlowCallbackRejectedError(
                        f"flow callback rejected: {err.code} {body!r}"
                    ) from err
                if attempt >= self.max_attempts:
                    raise FlowCallbackTemporaryError(
                        f"flow callback temporary failed: {err.code} {body!r}"
                    ) from err
                _sleep_before_retry(self.retry_backoff_seconds, attempt)
            except (TimeoutError, OSError, URLError, FlowCallbackTemporaryError) as err:
                last_error = err
                if attempt >= self.max_attempts:
                    raise
                _sleep_before_retry(self.retry_backoff_seconds, attempt)
        if last_error is not None:
            raise last_error
        return False


class FlowCallbackTemporaryError(RuntimeError):
    pass


class FlowCallbackRejectedError(RuntimeError):
    pass


def build_flow_callback_http_request(
    event: FlowCallbackEvent,
    *,
    base_url: str,
    path: str,
    client_id: str,
    secret: str,
    timestamp_seconds: int | None = None,
    nonce: str | None = None,
) -> FlowCallbackHttpRequest:
    resolved_timestamp = str(timestamp_seconds or int(time.time()))
    resolved_nonce = nonce or uuid.uuid4().hex
    raw_body = json.dumps(
        event.to_message(),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    body_hash = hashlib.sha256(raw_body).hexdigest()
    canonical = "\n".join(["POST", path, resolved_timestamp, resolved_nonce, body_hash])
    signature = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return FlowCallbackHttpRequest(
        url=base_url.rstrip("/") + path,
        raw_body=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-LC-Client-Id": client_id,
            "X-LC-Timestamp": resolved_timestamp,
            "X-LC-Nonce": resolved_nonce,
            "X-LC-Signature": signature,
            "X-LC-Signature-Path": path,
        },
    )


def build_flow_callback_event(
    context: Mapping[str, Any],
    *,
    status: str,
    message: str,
    business_id: str | None = None,
    timestamp: int | None = None,
) -> FlowCallbackEvent | None:
    if status not in FLOW_CALLBACK_STATUSES:
        raise ValueError(f"unsupported flow callback status: {status}")

    task_id = _context_text(context.get("taskId"))
    if task_id is None:
        LOGGER.warning("flow_callback_skipped_missing_task_id status=%s", status)
        return None

    resolved_business_id = (
        _context_text(context.get("businessId"))
        or _context_text(business_id)
        or _context_text(context.get("callId"))
        or task_id
    )
    assert resolved_business_id is not None
    return FlowCallbackEvent(
        tenant_id=_context_text(context.get("tenantId")) or "",
        task_id=task_id,
        business_id=resolved_business_id,
        status=status,
        message=message,
        timestamp=timestamp or int(time.time() * 1000),
    )


def _context_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _urlopen(request: Request, *, timeout: float):
    return urlopen(request, timeout=timeout)


def _raise_for_business_error(body: bytes) -> None:
    if not body:
        return
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(payload, Mapping) and payload.get("code") not in {None, 200}:
        raise FlowCallbackRejectedError(f"flow callback business failed: {payload}")


def _response_status(response: object) -> int:
    status = getattr(response, "status", None)
    if status is not None:
        return int(status)
    getcode = getattr(response, "getcode")
    return int(getcode())


def _sleep_before_retry(backoff_seconds: float, attempt: int) -> None:
    if backoff_seconds <= 0:
        return
    time.sleep(backoff_seconds * (2 ** max(0, attempt - 1)))
