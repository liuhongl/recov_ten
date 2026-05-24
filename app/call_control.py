from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import GatewayConfig, OutboundCallConfig
from .freeswitch_event_socket import (
    ChannelStateEvent,
    EventSocketError,
    FreeSwitchEventSocketClient,
)
from .opening import (
    OpeningAudioGenerator,
    OpeningAudioStore,
    OpeningCallMetadata,
    OpeningGenerationFailed,
    OpeningGenerationTimeout,
    OpeningRequest,
    build_prepared_opening_audio,
    parse_opening_request,
)
from .postgres import BusinessPromptPreparation, PromptSnapshot

LOGGER = logging.getLogger(__name__)

SAFE_TOKEN_RE = re.compile(r"^[^\s{},]+$")


class CallControlError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CreateCallRequest:
    destination: str
    external_call_id: str | None = None
    endpoint: str | None = None
    dialplan_extension: str | None = None
    dialplan_context: str | None = None
    caller_id_name: str | None = None
    caller_id_number: str | None = None
    originate_timeout_seconds: int | None = None
    context: dict[str, Any] = field(default_factory=dict)
    opening: OpeningRequest | None = None


@dataclass
class OutboundCallRecord:
    call_id: str
    destination: str
    endpoint: str
    dialplan_extension: str
    dialplan_context: str
    caller_id_name: str
    caller_id_number: str
    originate_timeout_seconds: int
    external_call_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    originate_completed_at_ms: int | None = None
    ringing_at_ms: int | None = None
    answered_at_ms: int | None = None
    media_connected_at_ms: int | None = None
    media_disconnected_at_ms: int | None = None
    freeswitch_reply: str | None = None
    error: str | None = None
    requested_endpoint: str | None = None
    hangup_cause: str | None = None
    sip_status: str | None = None
    sip_reason: str | None = None
    last_event_name: str | None = None
    last_event_at_ms: int | None = None
    opening: OpeningCallMetadata | None = None
    prompt_snapshot: PromptSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        diagnostics = _build_call_diagnostics(self)
        return {
            "call_id": self.call_id,
            "external_call_id": self.external_call_id,
            "destination": self.destination,
            "endpoint": self.endpoint,
            "requested_endpoint": self.requested_endpoint,
            "dialplan_extension": self.dialplan_extension,
            "dialplan_context": self.dialplan_context,
            "caller_id_name": self.caller_id_name,
            "caller_id_number": self.caller_id_number,
            "originate_timeout_seconds": self.originate_timeout_seconds,
            "context": self.context,
            "status": self.status,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "originate_completed_at_ms": self.originate_completed_at_ms,
            "ringing_at_ms": self.ringing_at_ms,
            "answered_at_ms": self.answered_at_ms,
            "media_connected_at_ms": self.media_connected_at_ms,
            "media_disconnected_at_ms": self.media_disconnected_at_ms,
            "freeswitch_reply": self.freeswitch_reply,
            "error": self.error,
            "hangup_cause": self.hangup_cause,
            "sip_status": self.sip_status,
            "sip_reason": self.sip_reason,
            "last_event_name": self.last_event_name,
            "last_event_at_ms": self.last_event_at_ms,
            "opening": None if self.opening is None else self.opening.to_dict(),
            "prompt": (
                None
                if self.prompt_snapshot is None
                else {
                    "scene": self.prompt_snapshot.scene,
                    "version": self.prompt_snapshot.version,
                    "content_hash": self.prompt_snapshot.content_hash,
                    "loaded_at_ms": self.prompt_snapshot.loaded_at_ms,
                    "metadata": self.prompt_snapshot.metadata,
                }
            ),
            **diagnostics,
        }


class FreeSwitchOutboundDialer:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    async def originate(self, command: str) -> str:
        client = self._make_client()
        try:
            await client.connect()
            return await client.api(command)
        finally:
            await client.close()

    async def resolve_endpoint(self, endpoint: str) -> str:
        if not endpoint.startswith("sofia_contact:"):
            return endpoint

        query = endpoint.removeprefix("sofia_contact:").strip()
        _require_safe_token(query, "endpoint")
        client = self._make_client()
        try:
            await client.connect()
            reply = (await client.api(f"sofia_contact {query}")).strip()
        finally:
            await client.close()

        if not reply or reply.startswith(("error/", "-ERR")):
            raise CallControlError(
                f"could not resolve FreeSWITCH contact for {query}: {reply}",
                status_code=503,
            )
        return reply

    async def hangup(self, call_id: str, *, cause: str) -> str:
        client = self._make_client()
        try:
            await client.connect()
            return await client.api(f"uuid_kill {call_id} {cause}")
        finally:
            await client.close()

    def _make_client(self) -> FreeSwitchEventSocketClient:
        event_socket = self.config.event_socket
        password = os.getenv(event_socket.password_env, "")
        if not password:
            raise CallControlError(
                f"missing Event Socket password env: {event_socket.password_env}",
                status_code=503,
            )
        return FreeSwitchEventSocketClient(
            host=event_socket.host,
            port=event_socket.port,
            password=password,
        )


DialerFactory = Callable[[], FreeSwitchOutboundDialer]


class BusinessPromptPreparerProtocol(Protocol):
    def prepare(self, context: dict[str, Any]) -> BusinessPromptPreparation | None: ...


class CallRecordUpdaterProtocol(Protocol):
    def mark_started(self, context: dict[str, Any]) -> bool: ...

    def mark_failed(self, context: dict[str, Any]) -> bool: ...

    def mark_no_answer(self, context: dict[str, Any]) -> bool: ...


class OutboundCallManager:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        dialer_factory: DialerFactory | None = None,
        opening_generator: OpeningAudioGenerator | None = None,
        opening_store: OpeningAudioStore | None = None,
        business_prompt_preparer: BusinessPromptPreparerProtocol | None = None,
        call_record_updater: CallRecordUpdaterProtocol | None = None,
    ) -> None:
        self.config = config
        self._dialer_factory = dialer_factory or (lambda: FreeSwitchOutboundDialer(config))
        self._opening_generator = opening_generator
        self._opening_store = opening_store
        self._business_prompt_preparer = business_prompt_preparer
        self._call_record_updater = call_record_updater
        self._calls: dict[str, OutboundCallRecord] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="outbound-call-control",
        )
        self._event_stop = threading.Event()
        self._event_future = None

    def start(self) -> None:
        if self._event_future is not None:
            return
        if not self.config.outbound.enabled or not self.config.event_socket.enabled:
            return
        self._event_stop.clear()
        self._event_future = self._executor.submit(self._run_event_listener_worker)

    def shutdown(self) -> None:
        self._event_stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def create_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.outbound.enabled:
            raise CallControlError("outbound calls are disabled", status_code=503)
        if not self.config.event_socket.enabled:
            raise CallControlError(
                "FreeSWITCH Event Socket is disabled; outbound calls require it",
                status_code=503,
            )

        request = parse_create_call_request(payload)
        record = self._build_record(request)
        business_opening = self._prepare_business_prompt(record)
        opening = business_opening or request.opening
        if opening is not None:
            self._prepare_opening(record, opening)

        with self._lock:
            self._calls[record.call_id] = record
            self._trim_locked()

        self._executor.submit(self._run_originate_worker, record.call_id)
        LOGGER.info(
            "outbound_call_queued call_id=%s destination=%s endpoint=%s",
            record.call_id,
            record.destination,
            record.endpoint,
        )
        return record.to_dict()

    def list_calls(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(
                self._calls.values(),
                key=lambda call: call.created_at_ms,
                reverse=True,
            )
            return [record.to_dict() for record in records[:limit]]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._calls.get(call_id)
            return None if record is None else record.to_dict()

    def get_prompt_snapshot(self, call_id: str) -> PromptSnapshot | None:
        with self._lock:
            record = self._calls.get(call_id)
            return None if record is None else record.prompt_snapshot

    def get_call_context(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._calls.get(call_id)
            return None if record is None else dict(record.context)

    def is_call_answered(self, call_id: str) -> bool:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return True
            return record.answered_at_ms is not None

    def handle_channel_event(self, event: ChannelStateEvent) -> None:
        sync_context: dict[str, Any] | None = None
        sync_status: str | None = None
        with self._lock:
            record = self._calls.get(event.call_id)
            if record is None:
                return
            was_terminal = _is_terminal_status(record.status)
            self._apply_channel_event_locked(record, event)
            if not was_terminal and _is_terminal_status(record.status):
                sync_context = dict(record.context)
                sync_status = record.status
        self._sync_call_record_terminal(sync_context, sync_status)

    def mark_media_connected(self, call_id: str) -> None:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or _is_terminal_status(record.status):
                return
            now_ms = _now_ms()
            record.media_connected_at_ms = record.media_connected_at_ms or now_ms
            self._set_status_locked(record, "media_connected")

    def mark_media_disconnected(self, call_id: str) -> None:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return
            record.media_disconnected_at_ms = record.media_disconnected_at_ms or _now_ms()
            record.updated_at_ms = _now_ms()

    def request_hangup(self, call_id: str, *, cause: str = "NORMAL_CLEARING") -> dict[str, Any]:
        _require_safe_token(cause, "cause")
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            self._set_status_locked(record, "hangup_requested")

        self._executor.submit(self._run_hangup_worker, call_id, cause)
        return record.to_dict()

    def _build_record(self, request: CreateCallRequest) -> OutboundCallRecord:
        outbound = self.config.outbound
        destination = request.destination
        endpoint = request.endpoint or _render_endpoint_template(
            outbound.endpoint_template,
            destination,
        )
        call_id = uuid.uuid4().hex
        caller_id_name = request.caller_id_name or outbound.caller_id_name
        caller_id_number = request.caller_id_number or outbound.caller_id_number
        _require_safe_token(caller_id_name, "caller_id_name")
        _require_safe_token(caller_id_number, "caller_id_number")
        _require_safe_token(outbound.dialplan_extension, "dialplan_extension")
        _require_safe_token(outbound.dialplan_context, "dialplan_context")

        return OutboundCallRecord(
            call_id=call_id,
            external_call_id=request.external_call_id,
            destination=destination,
            endpoint=endpoint,
            requested_endpoint=endpoint,
            dialplan_extension=(
                request.dialplan_extension or outbound.dialplan_extension
            ),
            dialplan_context=request.dialplan_context or outbound.dialplan_context,
            caller_id_name=caller_id_name,
            caller_id_number=caller_id_number,
            originate_timeout_seconds=(
                request.originate_timeout_seconds
                or outbound.originate_timeout_seconds
            ),
            context=request.context,
        )

    def _prepare_business_prompt(
        self,
        record: OutboundCallRecord,
    ) -> OpeningRequest | None:
        if self._business_prompt_preparer is None:
            return None

        preparation = self._business_prompt_preparer.prepare(record.context)
        if preparation is None:
            return None

        record.prompt_snapshot = preparation.prompt_snapshot
        LOGGER.info(
            "business_prompt_ready call_id=%s scene=%s version=%s content_hash=%s",
            record.call_id,
            preparation.prompt_snapshot.scene,
            preparation.prompt_snapshot.version,
            preparation.prompt_snapshot.content_hash,
        )
        return preparation.opening

    def _prepare_opening(
        self,
        record: OutboundCallRecord,
        opening: OpeningRequest,
    ) -> None:
        if self._opening_generator is None or self._opening_store is None:
            raise CallControlError(
                "opening generation is unavailable",
                status_code=503,
            )

        try:
            audio = self._opening_generator.generate(opening)
            prepared = build_prepared_opening_audio(
                call_id=record.call_id,
                opening=opening,
                audio=audio,
                config=self.config,
            )
        except OpeningGenerationTimeout as err:
            raise CallControlError(
                "opening_generation_timeout",
                status_code=504,
            ) from err
        except OpeningGenerationFailed as err:
            raise CallControlError(
                "opening_generation_failed",
                status_code=502,
            ) from err

        self._opening_store.put(prepared)
        record.opening = prepared.to_call_metadata()
        LOGGER.info(
            "opening_audio_ready call_id=%s text_hash=%s voice=%s "
            "generation_ms=%s audio_bytes=%s audio_sample_rate=%s phone_frames=%s",
            record.call_id,
            prepared.opening_text_hash,
            prepared.voice,
            prepared.generation_ms,
            prepared.source_audio_bytes,
            prepared.source_sample_rate,
            len(prepared.phone_frames),
        )

    def _run_originate_worker(self, call_id: str) -> None:
        try:
            asyncio.run(self._originate(call_id))
        except Exception:
            LOGGER.exception("outbound_call_worker_failed call_id=%s", call_id)
            self._mark_failed(call_id, "internal outbound call worker error")

    def _run_event_listener_worker(self) -> None:
        try:
            asyncio.run(self._event_listener_loop())
        except Exception:
            LOGGER.exception("outbound_call_event_listener_stopped")

    async def _event_listener_loop(self) -> None:
        while not self._event_stop.is_set():
            client: FreeSwitchEventSocketClient | None = None
            try:
                client = _make_event_socket_client(self.config)
                await client.connect()
                await client.subscribe_channel_events()
                LOGGER.info("outbound_call_event_listener_started")
                while not self._event_stop.is_set():
                    try:
                        event = await asyncio.wait_for(
                            client.read_channel_event(),
                            timeout=0.5,
                        )
                    except TimeoutError:
                        continue
                    self.handle_channel_event(event)
            except (OSError, EOFError, EventSocketError, CallControlError):
                LOGGER.warning(
                    "outbound_call_event_listener_reconnect",
                    exc_info=True,
                )
                await _sleep_unless_stopped(self._event_stop, 1.0)
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.close()

    async def _originate(self, call_id: str) -> None:
        with self._lock:
            record = self._calls[call_id]
            self._set_status_locked(record, "originating")
            record.started_at_ms = _now_ms()
            started_context = dict(record.context)

        self._sync_call_record_started(started_context)

        dialer = self._dialer_factory()
        try:
            resolved_endpoint = await dialer.resolve_endpoint(record.endpoint)
        except CallControlError as err:
            with self._lock:
                record = self._calls[call_id]
                record.error = str(err)
                record.completed_at_ms = _now_ms()
                self._set_status_locked(record, "failed")
                self._discard_opening_locked(record.call_id)
                failed_context = dict(record.context)
            self._sync_call_record_failed(failed_context)
            LOGGER.info(
                "outbound_call_endpoint_resolve_failed call_id=%s endpoint=%s error=%s",
                call_id,
                record.endpoint,
                err,
            )
            return

        with self._lock:
            record = self._calls[call_id]
            if resolved_endpoint != record.endpoint:
                record.endpoint = resolved_endpoint
            command = build_originate_command(record)

        LOGGER.info("outbound_call_originate_started call_id=%s", call_id)
        reply = await dialer.originate(command)
        stripped = reply.strip()
        failed_context = None
        with self._lock:
            record = self._calls[call_id]
            record.freeswitch_reply = stripped
            if stripped.startswith("-ERR"):
                record.error = stripped
                record.hangup_cause = _extract_failure_cause(stripped)
                record.completed_at_ms = _now_ms()
                self._set_status_locked(record, "failed")
                self._discard_opening_locked(record.call_id)
                failed_context = dict(record.context)
            else:
                if record.status in {"originating", "queued"}:
                    self._set_status_locked(record, "originated")
            record.originate_completed_at_ms = _now_ms()
        self._sync_call_record_failed(failed_context)
        LOGGER.info(
            "outbound_call_originate_finished call_id=%s status=%s reply=%s",
            call_id,
            self._calls[call_id].status,
            stripped,
        )

    def _run_hangup_worker(self, call_id: str, cause: str) -> None:
        try:
            asyncio.run(self._hangup(call_id, cause=cause))
        except Exception:
            LOGGER.exception("outbound_call_hangup_worker_failed call_id=%s", call_id)
            self._mark_failed(call_id, "internal hangup worker error")

    async def _hangup(self, call_id: str, *, cause: str) -> None:
        reply = await self._dialer_factory().hangup(call_id, cause=cause)
        stripped = reply.strip()
        failed_context = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return
            record.freeswitch_reply = stripped
            if stripped.startswith("-ERR"):
                record.error = stripped
                if "No such channel" in stripped:
                    record.completed_at_ms = record.completed_at_ms or _now_ms()
                self._set_status_locked(record, "hangup_failed")
                failed_context = dict(record.context)
            else:
                self._set_status_locked(record, "hangup_sent")
        self._sync_call_record_failed(failed_context)

    def _mark_failed(self, call_id: str, error: str) -> None:
        failed_context = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return
            record.error = error
            record.completed_at_ms = record.completed_at_ms or _now_ms()
            self._set_status_locked(record, "failed")
            self._discard_opening_locked(record.call_id)
            failed_context = dict(record.context)
        self._sync_call_record_failed(failed_context)

    def _apply_channel_event_locked(
        self,
        record: OutboundCallRecord,
        event: ChannelStateEvent,
    ) -> None:
        now_ms = _now_ms()
        record.last_event_name = event.name
        record.last_event_at_ms = now_ms
        if event.hangup_cause:
            record.hangup_cause = event.hangup_cause
        if event.sip_status:
            record.sip_status = event.sip_status
        if event.sip_reason:
            record.sip_reason = event.sip_reason

        if _is_terminal_status(record.status):
            record.updated_at_ms = now_ms
            return

        if event.name in {"CHANNEL_PROGRESS", "CHANNEL_PROGRESS_MEDIA"}:
            record.ringing_at_ms = record.ringing_at_ms or now_ms
            if record.status in {"queued", "originating", "originated"}:
                self._set_status_locked(record, "ringing")
            return

        if event.name == "CHANNEL_ANSWER":
            record.answered_at_ms = record.answered_at_ms or now_ms
            if record.status in {"queued", "originating", "originated", "ringing"}:
                self._set_status_locked(record, "answered")
            return

        if event.name in {"CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"}:
            record.completed_at_ms = record.completed_at_ms or now_ms
            self._set_status_locked(record, _terminal_status_for_cause(record))
            self._discard_opening_locked(record.call_id)

    def _set_status_locked(self, record: OutboundCallRecord, status: str) -> None:
        record.status = status
        record.updated_at_ms = _now_ms()

    def _trim_locked(self) -> None:
        max_recent_calls = max(1, self.config.outbound.max_recent_calls)
        if len(self._calls) <= max_recent_calls:
            return
        records = sorted(self._calls.values(), key=lambda call: call.created_at_ms)
        for record in records[: len(self._calls) - max_recent_calls]:
            self._calls.pop(record.call_id, None)
            self._discard_opening_locked(record.call_id)

    def _discard_opening_locked(self, call_id: str) -> None:
        if self._opening_store is not None:
            self._opening_store.discard(call_id)

    def _sync_call_record_started(self, context: dict[str, Any] | None) -> None:
        if self._call_record_updater is None or context is None:
            return
        try:
            self._call_record_updater.mark_started(context)
        except Exception:
            LOGGER.warning("call_record_started_sync_failed", exc_info=True)

    def _sync_call_record_failed(self, context: dict[str, Any] | None) -> None:
        if self._call_record_updater is None or context is None:
            return
        try:
            self._call_record_updater.mark_failed(context)
        except Exception:
            LOGGER.warning("call_record_failed_sync_failed", exc_info=True)

    def _sync_call_record_no_answer(self, context: dict[str, Any] | None) -> None:
        if self._call_record_updater is None or context is None:
            return
        try:
            self._call_record_updater.mark_no_answer(context)
        except Exception:
            LOGGER.warning("call_record_no_answer_sync_failed", exc_info=True)

    def _sync_call_record_terminal(
        self,
        context: dict[str, Any] | None,
        status: str | None,
    ) -> None:
        if status == "no_answer":
            self._sync_call_record_no_answer(context)
            return
        if status in {"failed", "busy", "canceled", "hangup_failed"}:
            self._sync_call_record_failed(context)


def parse_create_call_request(payload: dict[str, Any]) -> CreateCallRequest:
    if not isinstance(payload, dict):
        raise CallControlError("request body must be a JSON object")

    destination = _required_str(payload, "destination")
    _require_safe_token(destination, "destination")

    endpoint = _optional_str(payload, "endpoint")
    if endpoint is not None:
        _require_safe_token(endpoint, "endpoint")

    dialplan_extension = _optional_str(payload, "dialplan_extension")
    if dialplan_extension is not None:
        _require_safe_token(dialplan_extension, "dialplan_extension")

    dialplan_context = _optional_str(payload, "dialplan_context")
    if dialplan_context is not None:
        _require_safe_token(dialplan_context, "dialplan_context")

    timeout = _optional_int(payload, "originate_timeout_seconds")
    if timeout is not None and not 1 <= timeout <= 300:
        raise CallControlError("originate_timeout_seconds must be between 1 and 300")

    context = payload.get("context", {})
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise CallControlError("context must be a JSON object")

    try:
        opening = parse_opening_request(payload.get("opening"))
    except OpeningGenerationFailed as err:
        raise CallControlError(str(err)) from err

    return CreateCallRequest(
        destination=destination,
        external_call_id=_optional_safe_str(payload, "external_call_id"),
        endpoint=endpoint,
        dialplan_extension=dialplan_extension,
        dialplan_context=dialplan_context,
        caller_id_name=_optional_safe_str(payload, "caller_id_name"),
        caller_id_number=_optional_safe_str(payload, "caller_id_number"),
        originate_timeout_seconds=timeout,
        context=context,
        opening=opening,
    )


def build_originate_command(record: OutboundCallRecord) -> str:
    variables = {
        "origination_uuid": record.call_id,
        "origination_caller_id_name": record.caller_id_name,
        "origination_caller_id_number": record.caller_id_number,
        "originate_timeout": str(record.originate_timeout_seconds),
        "hangup_after_bridge": "true",
        "ignore_early_media": "true",
        "sip_realtime_gateway_call_id": record.call_id,
    }
    if record.external_call_id:
        variables["sip_realtime_external_call_id"] = record.external_call_id

    return (
        f"originate {_format_originate_variables(variables)}{record.endpoint} "
        f"{record.dialplan_extension} XML {record.dialplan_context}"
    )


def _make_event_socket_client(config: GatewayConfig) -> FreeSwitchEventSocketClient:
    event_socket = config.event_socket
    password = os.getenv(event_socket.password_env, "")
    if not password:
        raise CallControlError(
            f"missing Event Socket password env: {event_socket.password_env}",
            status_code=503,
        )
    return FreeSwitchEventSocketClient(
        host=event_socket.host,
        port=event_socket.port,
        password=password,
    )


async def _sleep_unless_stopped(stop_event: threading.Event, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)


def _build_call_diagnostics(record: OutboundCallRecord) -> dict[str, Any]:
    raw_cause = (
        record.hangup_cause
        or _extract_failure_cause(record.error)
        or _failure_cause_from_sip_status(record.sip_status)
    )
    hangup_cause = _normalize_failure_cause(record, raw_cause)
    failure_reason = _failure_reason(record, hangup_cause)
    failure = _failure_details(failure_reason)
    return {
        "phase": _phase(record.status, hangup_cause),
        "phase_label": _phase_label(record.status, hangup_cause),
        "failure_reason": failure_reason,
        "failure_label": failure["label"],
        "failure_hint": failure["hint"],
        "sip_status_hint": failure["sip_status_hint"],
        "elapsed_ms": _elapsed_ms(record),
        "originate_elapsed_ms": _duration_ms(
            record.started_at_ms,
            record.originate_completed_at_ms,
        ),
        "answer_latency_ms": _duration_ms(record.started_at_ms, record.answered_at_ms),
        "ringing_ms": _ringing_ms(record),
        "talk_duration_ms": _talk_duration_ms(record),
    }


def _extract_failure_cause(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.startswith("-ERR "):
        return stripped.removeprefix("-ERR ").strip() or None
    if stripped.startswith("+OK"):
        return None
    return stripped or None


def _failure_cause_from_sip_status(sip_status: str | None) -> str | None:
    if sip_status in {"408", "480"}:
        return "NO_ANSWER"
    if sip_status == "508":
        return "SIP_508"
    return None


def _normalize_failure_cause(
    record: OutboundCallRecord,
    cause: str | None,
) -> str | None:
    if record.sip_status in {"408", "480"}:
        return "NO_ANSWER"
    return cause


def _failure_reason(record: OutboundCallRecord, cause: str | None) -> str | None:
    if cause == "NORMAL_CLEARING" and record.status == "completed":
        return None
    return cause


def _failure_details(cause: str | None) -> dict[str, str | None]:
    if cause == "USER_BUSY":
        return {
            "label": "对端忙线或拒接",
            "hint": "软电话或线路已收到 INVITE，但返回忙线/拒接；本地测试时确认 Linphone、Zoiper 或 MicroSIP 未占线，点发起后及时接听。",
            "sip_status_hint": "486",
        }
    if cause == "CALL_REJECTED":
        return {
            "label": "对端拒接",
            "hint": "被叫端明确拒绝本次呼叫；真实线路下应结合运营商 CDR 或 SIP trace 确认。",
            "sip_status_hint": "603",
        }
    if cause == "NORMAL_TEMPORARY_FAILURE":
        return {
            "label": "临时失败",
            "hint": "通常是 SIP 503 或本地 NAT/软电话 Contact 瞬时不可用；刷新软电话注册或重启客户端后重试。",
            "sip_status_hint": "503",
        }
    if cause in {"NORMAL_UNSPECIFIED", "SIP_508"}:
        return {
            "label": "线路或上游未明原因失败",
            "hint": "真实 sip-provider 日志中该原因可能伴随 SIP 508 或 Q.850 cause=31；优先检查供应商 SBC、线路路由、公网 NAT/RTP 和运营商 CDR。",
            "sip_status_hint": "508",
        }
    if cause == "USER_NOT_REGISTERED":
        return {
            "label": "用户未注册",
            "hint": "本地分机或真实线路目标不可达；确认分机注册、SIP trunk 路由和拨号格式。",
            "sip_status_hint": "404",
        }
    if cause == "NO_ANSWER":
        return {
            "label": "无人接听",
            "hint": "外呼已送达但在超时时间内未接听。",
            "sip_status_hint": None,
        }
    if cause == "ORIGINATOR_CANCEL":
        return {
            "label": "主叫取消",
            "hint": "外呼流程被网关或调用方取消。",
            "sip_status_hint": None,
        }
    if cause == "NORMAL_CLEARING":
        return {"label": None, "hint": None, "sip_status_hint": None}
    if cause:
        return {
            "label": cause,
            "hint": "查看 FreeSWITCH SIP trace 或运营商 CDR 确认最终 SIP 返回码。",
            "sip_status_hint": None,
        }
    return {"label": None, "hint": None, "sip_status_hint": None}


def _phase(status: str, cause: str | None) -> str:
    if status in {"failed", "busy", "no_answer", "canceled"}:
        if cause == "USER_BUSY":
            return "busy"
        if cause == "CALL_REJECTED":
            return "busy"
        if cause == "NORMAL_TEMPORARY_FAILURE":
            return "temporary_failure"
        if cause in {"NORMAL_UNSPECIFIED", "SIP_508"}:
            return "trunk_or_upstream_failure"
        if cause == "NO_ANSWER":
            return "no_answer"
        if cause == "ORIGINATOR_CANCEL":
            return "canceled"
        if status != "failed":
            return status
        return "failed"
    if status == "queued":
        return "queued"
    if status == "originating":
        return "dialing"
    if status == "ringing":
        return "ringing"
    if status == "originated":
        return "answered"
    if status == "answered":
        return "answered"
    if status == "media_connected":
        return "media_connected"
    if status == "completed":
        return "completed"
    if status == "media_disconnected":
        return "media_disconnected"
    if status == "hangup_requested":
        return "hangup_requested"
    if status == "hangup_sent":
        return "hangup_sent"
    if status == "hangup_failed":
        return "hangup_failed"
    return status


def _phase_label(status: str, cause: str | None) -> str:
    phase = _phase(status, cause)
    labels = {
        "queued": "已排队",
        "dialing": "呼叫中",
        "ringing": "振铃中",
        "answered": "已接通",
        "media_connected": "AI 媒体已接入",
        "media_disconnected": "媒体已断开",
        "completed": "已结束",
        "busy": "忙线/拒接",
        "temporary_failure": "临时失败",
        "trunk_or_upstream_failure": "线路或上游失败",
        "no_answer": "无人接听",
        "canceled": "已取消",
        "failed": "失败",
        "hangup_requested": "挂断中",
        "hangup_sent": "已发送挂断",
        "hangup_failed": "挂断失败",
    }
    return labels.get(phase, status)


def _elapsed_ms(record: OutboundCallRecord) -> int | None:
    started_at_ms = record.started_at_ms or record.created_at_ms
    ended_at_ms = record.completed_at_ms
    if ended_at_ms is None:
        if record.status not in {"failed"}:
            ended_at_ms = _now_ms()
        else:
            return None
    return max(0, ended_at_ms - started_at_ms)


def _duration_ms(started_at_ms: int | None, ended_at_ms: int | None) -> int | None:
    if started_at_ms is None or ended_at_ms is None:
        return None
    return max(0, ended_at_ms - started_at_ms)


def _ringing_ms(record: OutboundCallRecord) -> int | None:
    if record.ringing_at_ms is None:
        return None
    ended_at_ms = record.answered_at_ms or record.completed_at_ms
    if ended_at_ms is None and not _is_terminal_status(record.status):
        ended_at_ms = _now_ms()
    return _duration_ms(record.ringing_at_ms, ended_at_ms)


def _talk_duration_ms(record: OutboundCallRecord) -> int | None:
    if record.answered_at_ms is None:
        return None
    ended_at_ms = record.completed_at_ms
    if ended_at_ms is None and not _is_terminal_status(record.status):
        ended_at_ms = _now_ms()
    return _duration_ms(record.answered_at_ms, ended_at_ms)


def _terminal_status_for_cause(record: OutboundCallRecord) -> str:
    cause = record.hangup_cause or _extract_failure_cause(record.error)
    if cause in {None, "NORMAL_CLEARING"}:
        return "completed" if record.answered_at_ms or record.media_connected_at_ms else "canceled"
    if cause in {"USER_BUSY", "CALL_REJECTED"}:
        return "busy"
    if cause == "NO_ANSWER":
        return "no_answer"
    if cause == "ORIGINATOR_CANCEL":
        return "canceled"
    return "failed"


def _is_terminal_status(status: str) -> bool:
    return status in {
        "completed",
        "failed",
        "busy",
        "no_answer",
        "canceled",
        "hangup_failed",
    }


def _format_originate_variables(variables: dict[str, str]) -> str:
    parts = []
    for key, value in variables.items():
        _require_safe_token(key, key)
        parts.append(f"{key}={_escape_variable_value(value)}")
    return "{" + ",".join(parts) + "}"


def _escape_variable_value(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def _required_str(payload: dict[str, Any], name: str) -> str:
    value = _optional_str(payload, name)
    if value is None:
        raise CallControlError(f"{name} is required")
    return value


def _optional_str(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CallControlError(f"{name} must be a string")
    value = value.strip()
    return value or None


def _optional_safe_str(payload: dict[str, Any], name: str) -> str | None:
    value = _optional_str(payload, name)
    if value is not None:
        _require_safe_token(value, name)
    return value


def _render_endpoint_template(template: str, destination: str) -> str:
    return template.replace("{destination}", destination)


def _optional_int(payload: dict[str, Any], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise CallControlError(f"{name} must be an integer") from err


def _require_safe_token(value: str, name: str) -> None:
    if not SAFE_TOKEN_RE.match(value):
        raise CallControlError(f"{name} contains unsupported characters")


def _now_ms() -> int:
    return int(time.time() * 1000)
