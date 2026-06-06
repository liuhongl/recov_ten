from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from .config import CallRecordingConfig, GatewayConfig, OutboundCallConfig
from .freeswitch_event_socket import (
    ChannelStateEvent,
    EventSocketError,
    FreeSwitchEventSocketClient,
)
from .flow_callback import FlowCallbackWriterProtocol, build_flow_callback_event
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
from .wav_io import write_pcm16_wav

LOGGER = logging.getLogger(__name__)

SAFE_TOKEN_RE = re.compile(r"^[^\s{},]+$")
LOCAL_PLACEHOLDER_BUSINESS_ID_PREFIX = "handoff-local"
HANDOFF_AGENT_BUSY_PROMPT_TEXT = (
    "抱歉，当前人工座席繁忙，稍后我们会继续跟进，感谢您的理解。"
)


class CallControlError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CreateCallRequest:
    destination: str | None
    external_call_id: str | None = None
    endpoint: str | None = None
    dialplan_extension: str | None = None
    dialplan_context: str | None = None
    caller_id_name: str | None = None
    caller_id_number: str | None = None
    originate_timeout_seconds: int | None = None
    context: dict[str, Any] = field(default_factory=dict)
    opening: OpeningRequest | None = None


@dataclass(frozen=True)
class HandoffRequest:
    trigger: str | None = None
    reason: str | None = None
    last_utterance: str | None = None
    summary: str | None = None
    ai_turns: list[dict[str, Any]] = field(default_factory=list)
    wait_timeout_seconds: int = 60


@dataclass(frozen=True)
class HandoffClaimRequest:
    agent_extension: str
    agent_uuid: str
    timeout_seconds: int
    claimed_by: str | None = None


@dataclass
class HandoffState:
    state: str
    requested_at_ms: int
    updated_at_ms: int
    expires_at_ms: int | None = None
    trigger: str | None = None
    reason: str | None = None
    last_utterance: str | None = None
    summary: str | None = None
    claimed_at_ms: int | None = None
    claimed_by: str | None = None
    agent_extension: str | None = None
    agent_uuid: str | None = None
    agent_endpoint: str | None = None
    answered_at_ms: int | None = None
    bridged_at_ms: int | None = None
    human_ended_at_ms: int | None = None
    agent_originate_reply: str | None = None
    audio_stream_break_reply: str | None = None
    bridge_reply: str | None = None
    human_transcript_status: str | None = None
    human_transcript_error: str | None = None
    terminal_callback_status: str | None = None
    recording_status: str | None = None
    recording_error: str | None = None
    customer_recording_path: str | None = None
    agent_recording_path: str | None = None
    customer_recording_host_path: str | None = None
    agent_recording_host_path: str | None = None
    recording_started_at_ms: int | None = None
    recording_stopped_at_ms: int | None = None
    ai_turns: list[dict[str, Any]] = field(default_factory=list)
    human_turns: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        turns = [*self.ai_turns, *self.human_turns]
        return {
            "state": self.state,
            "trigger": self.trigger,
            "reason": self.reason,
            "last_utterance": self.last_utterance,
            "summary": self.summary,
            "requested_at_ms": self.requested_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "claimed_at_ms": self.claimed_at_ms,
            "claimed_by": self.claimed_by,
            "agent_extension": self.agent_extension,
            "agent_uuid": self.agent_uuid,
            "agent_endpoint": self.agent_endpoint,
            "answered_at_ms": self.answered_at_ms,
            "bridged_at_ms": self.bridged_at_ms,
            "human_ended_at_ms": self.human_ended_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "agent_originate_reply": self.agent_originate_reply,
            "audio_stream_break_reply": self.audio_stream_break_reply,
            "bridge_reply": self.bridge_reply,
            "human_transcript_status": self.human_transcript_status,
            "human_transcript_error": self.human_transcript_error,
            "recording_status": self.recording_status,
            "recording_error": self.recording_error,
            "customer_recording_path": self.customer_recording_path,
            "agent_recording_path": self.agent_recording_path,
            "customer_recording_host_path": self.customer_recording_host_path,
            "agent_recording_host_path": self.agent_recording_host_path,
            "recording_started_at_ms": self.recording_started_at_ms,
            "recording_stopped_at_ms": self.recording_stopped_at_ms,
            "ai_turns": list(self.ai_turns),
            "human_turns": list(self.human_turns),
            "turns": turns,
            "recent_turns": turns[-8:],
            "can_claim": self.state == "waiting_agent"
            and (self.expires_at_ms is None or self.expires_at_ms > _now_ms()),
            "error": self.error,
        }


@dataclass
class AgentTakeoverSuggestion:
    state: str
    suggested_at_ms: int
    updated_at_ms: int
    reason: str | None = None
    last_utterance: str | None = None

    def to_dict(self, *, can_takeover: bool) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "last_utterance": self.last_utterance,
            "suggested_at_ms": self.suggested_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "can_takeover": can_takeover,
        }


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
    handoff: HandoffState | None = None
    agent_takeover_suggestion: AgentTakeoverSuggestion | None = None
    recording_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        diagnostics = _build_call_diagnostics(self)
        handoff_payload = None if self.handoff is None else self.handoff.to_dict()
        takeover_suggestion_payload = (
            None
            if self.agent_takeover_suggestion is None
            else self.agent_takeover_suggestion.to_dict(
                can_takeover=_can_takeover_from_suggestion(self)
            )
        )
        turns = [] if handoff_payload is None else handoff_payload["turns"]
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
            "recording_path": self.recording_path,
            "handoff": handoff_payload,
            "agent_takeover_suggestion": takeover_suggestion_payload,
            "turns": turns,
            "recent_turns": turns[-8:],
            "summary": _handoff_summary(self.handoff),
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

    async def break_audio_stream(self, call_id: str) -> str:
        command = build_handoff_audio_stream_stop_command(call_id=call_id)
        client = self._make_client()
        try:
            await client.connect()
            return await client.api(command)
        finally:
            await client.close()

    async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
        command = build_uuid_bridge_command(
            customer_call_id=customer_call_id,
            agent_uuid=agent_uuid,
        )
        client = self._make_client()
        try:
            await client.connect()
            return await client.api(command)
        finally:
            await client.close()

    async def start_recording(
        self,
        channel_uuid: str,
        path: str,
        *,
        read_only: bool = False,
    ) -> str:
        return await self._record(channel_uuid, "start", path, read_only=read_only)

    async def stop_recording(self, channel_uuid: str, path: str) -> str:
        return await self._record(channel_uuid, "stop", path)

    async def play_file(self, call_id: str, path: str) -> str:
        _require_safe_token(call_id, "call_id")
        _require_safe_token(path, "playback_path")
        client = self._make_client()
        try:
            await client.connect()
            return await client.api(f"uuid_broadcast {call_id} {path} aleg")
        finally:
            await client.close()

    async def _record(
        self,
        channel_uuid: str,
        action: str,
        path: str,
        *,
        read_only: bool = False,
    ) -> str:
        _require_safe_token(channel_uuid, "channel_uuid")
        _require_safe_token(action, "recording_action")
        _require_safe_token(path, "recording_path")
        client = self._make_client()
        try:
            await client.connect()
            if action == "start" and read_only:
                for command in (
                    f"uuid_setvar {channel_uuid} RECORD_READ_ONLY true",
                    f"uuid_setvar {channel_uuid} RECORD_WRITE_ONLY false",
                ):
                    reply = await client.api(command)
                    if reply.strip().startswith("-ERR"):
                        return reply
            return await client.api(f"uuid_record {channel_uuid} {action} {path}")
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


def build_webrtc_agent_originate_command(
    *,
    agent_uuid: str,
    endpoint: str,
    timeout_seconds: int,
) -> str:
    _require_safe_token(agent_uuid, "agent_uuid")
    _require_safe_token(endpoint, "endpoint")
    if not 1 <= timeout_seconds <= 120:
        raise CallControlError("timeout_seconds must be between 1 and 120")
    variables = {
        "origination_uuid": agent_uuid,
        "origination_caller_id_name": "Handoff_Test",
        "origination_caller_id_number": "9001",
        "originate_timeout": str(timeout_seconds),
    }
    return f"originate {_format_originate_variables(variables)}{endpoint} &park()"


def build_uuid_bridge_command(*, customer_call_id: str, agent_uuid: str) -> str:
    _require_safe_token(customer_call_id, "customer_call_id")
    _require_safe_token(agent_uuid, "agent_uuid")
    return f"uuid_bridge {customer_call_id} {agent_uuid}"


def build_handoff_audio_stream_stop_command(*, call_id: str) -> str:
    _require_safe_token(call_id, "call_id")
    return f"uuid_audio_stream {call_id} stop"


def _handoff_recording_paths(
    recording_dir: str,
    recording_host_dir: str,
    *,
    customer_call_id: str,
    agent_uuid: str,
) -> tuple[str, str, str, str]:
    base_dir = recording_dir.rstrip("/")
    _require_safe_token(base_dir, "recording_dir")
    host_base_dir = recording_host_dir.rstrip("/")
    if host_base_dir:
        _require_safe_token(host_base_dir, "recording_host_dir")
    else:
        host_base_dir = base_dir
    _require_safe_token(customer_call_id, "customer_call_id")
    _require_safe_token(agent_uuid, "agent_uuid")
    customer_filename = f"{customer_call_id}-customer.wav"
    agent_filename = f"{customer_call_id}-{agent_uuid}-agent.wav"
    return (
        f"{base_dir}/{customer_filename}",
        f"{base_dir}/{agent_filename}",
        f"{host_base_dir}/{customer_filename}",
        f"{host_base_dir}/{agent_filename}",
    )


def _recording_stop_reply_is_terminal_ok(reply: str) -> bool:
    return reply.strip().startswith("-ERR Cannot locate session")


def originate_webrtc_agent_test_call(
    config: GatewayConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not config.event_socket.enabled:
        raise CallControlError(
            "FreeSWITCH Event Socket is disabled; WebRTC agent test calls require it",
            status_code=503,
        )

    agent_extension = _optional_safe_str(payload, "agent_extension") or "1001"
    timeout_seconds = _optional_int(payload, "timeout_seconds") or 20
    if not 1 <= timeout_seconds <= 120:
        raise CallControlError("timeout_seconds must be between 1 and 120")
    agent_uuid = _optional_safe_str(payload, "agent_uuid") or uuid.uuid4().hex

    async def run() -> dict[str, Any]:
        dialer = FreeSwitchOutboundDialer(config)
        endpoint = await dialer.resolve_endpoint(f"sofia_contact:*/{agent_extension}")
        command = build_webrtc_agent_originate_command(
            agent_uuid=agent_uuid,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
        )
        reply = (await dialer.originate(command)).strip()
        if reply.startswith("-ERR"):
            raise CallControlError(reply, status_code=503)
        return {
            "agent_uuid": agent_uuid,
            "agent_extension": agent_extension,
            "endpoint": endpoint,
            "freeswitch_reply": reply,
        }

    try:
        return asyncio.run(run())
    except (OSError, EOFError, EventSocketError) as err:
        raise CallControlError(
            f"FreeSWITCH Event Socket request failed: {err}",
            status_code=503,
        ) from err


DialerFactory = Callable[[], FreeSwitchOutboundDialer]


class BusinessPromptPreparerProtocol(Protocol):
    def prepare(self, context: dict[str, Any]) -> BusinessPromptPreparation | None: ...


class CallRecordUpdaterProtocol(Protocol):
    def mark_started(self, context: dict[str, Any]) -> bool: ...

    def mark_failed(self, context: dict[str, Any]) -> bool: ...

    def mark_no_answer(self, context: dict[str, Any]) -> bool: ...


class CallResultWriterProtocol(Protocol):
    def enqueue_nowait(self, payload: dict[str, Any]) -> bool: ...


class HumanHandoffTranscriptProcessorProtocol(Protocol):
    def process(self, job: dict[str, Any]) -> list[dict[str, Any]]: ...


class CallDestinationResolverProtocol(Protocol):
    def resolve(self, context: dict[str, Any]) -> str | None: ...


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
        call_result_writer: CallResultWriterProtocol | None = None,
        flow_callback_writer: FlowCallbackWriterProtocol | None = None,
        destination_resolver: CallDestinationResolverProtocol | None = None,
        handoff_transcript_processor: (
            HumanHandoffTranscriptProcessorProtocol | None
        ) = None,
    ) -> None:
        self.config = config
        self._dialer_factory = dialer_factory or (lambda: FreeSwitchOutboundDialer(config))
        self._opening_generator = opening_generator
        self._opening_store = opening_store
        self._business_prompt_preparer = business_prompt_preparer
        self._call_record_updater = call_record_updater
        self._call_result_writer = call_result_writer
        self._flow_callback_writer = flow_callback_writer
        self._destination_resolver = destination_resolver
        self._handoff_transcript_processor = handoff_transcript_processor
        self._calls: dict[str, OutboundCallRecord] = {}
        self._external_call_index: dict[str, str] = {}
        self._handoff_timeout_timers: dict[str, threading.Timer] = {}
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
        with self._lock:
            timers = list(self._handoff_timeout_timers.values())
            self._handoff_timeout_timers.clear()
        for timer in timers:
            timer.cancel()
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
        self._validate_flow_callback_context(request.context)
        self._validate_flow_callback_runtime_ready()
        request = self._resolve_call_destination(request)
        idempotency_key = _idempotency_key(request)
        if idempotency_key is not None:
            with self._lock:
                existing = self._record_for_idempotency_key_locked(idempotency_key)
                if existing is not None:
                    LOGGER.info(
                        "outbound_call_idempotent_accept idempotency_key=%s "
                        "call_id=%s status=%s",
                        idempotency_key,
                        existing.call_id,
                        existing.status,
                    )
                    return existing.to_dict()

        record = self._build_record(request)

        business_opening = self._prepare_business_prompt(record)
        opening = business_opening or request.opening
        if opening is not None:
            self._prepare_opening(record, opening)

        with self._lock:
            if idempotency_key is not None:
                existing = self._record_for_idempotency_key_locked(idempotency_key)
                if existing is not None:
                    LOGGER.info(
                        "outbound_call_idempotent_accept idempotency_key=%s "
                        "call_id=%s status=%s",
                        idempotency_key,
                        existing.call_id,
                        existing.status,
                    )
                    return existing.to_dict()
            self._calls[record.call_id] = record
            if idempotency_key is not None:
                self._external_call_index[idempotency_key] = record.call_id
            self._trim_locked()

        self._publish_flow_callback(
            record.context,
            status="ACCEPTED",
            message="外呼任务已受理",
            business_id=_business_id(record),
        )
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

    def get_call_recording_path(self, call_id: str) -> str | None:
        with self._lock:
            record = self._calls.get(call_id)
            return None if record is None else record.recording_path

    def is_call_answered(self, call_id: str) -> bool:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return True
            return record.answered_at_ms is not None

    def handle_channel_event(self, event: ChannelStateEvent) -> None:
        sync_context: dict[str, Any] | None = None
        sync_business_id: str | None = None
        sync_status: str | None = None
        stop_handoff_recording_call_id: str | None = None
        cancel_handoff_timeout_call_id: str | None = None
        cleanup_handoff_agent: tuple[str, str] | None = None
        handoff_failed_callback: tuple[dict[str, Any], str | None] | None = None
        handoff_connection_failed_callback: tuple[
            dict[str, Any],
            str | None,
        ] | None = None
        with self._lock:
            record = self._calls.get(event.call_id)
            if record is None:
                return
            was_terminal = _is_terminal_status(record.status)
            self._apply_channel_event_locked(record, event)
            if not was_terminal and _is_terminal_status(record.status):
                sync_context = dict(record.context)
                sync_business_id = _business_id(record)
                sync_status = record.status
                if record.handoff is not None:
                    cancel_handoff_timeout_call_id = record.call_id
                    if (
                        record.handoff.state == "completed"
                        and record.handoff.agent_uuid
                    ):
                        cleanup_handoff_agent = (
                            record.call_id,
                            record.handoff.agent_uuid,
                        )
            if (
                record.handoff is not None
                and record.handoff.state == "completed"
                and record.handoff.recording_status == "recording"
            ):
                record.handoff.recording_status = "stopping"
                record.handoff.updated_at_ms = _now_ms()
                stop_handoff_recording_call_id = record.call_id
            handoff_failed_callback = self._handoff_failed_callback_locked(record)
            handoff_connection_failed_callback = (
                self._handoff_connection_failed_callback_locked(record)
            )
        if stop_handoff_recording_call_id is not None:
            self._executor.submit(
                self._run_stop_handoff_recording_worker,
                stop_handoff_recording_call_id,
            )
        if cancel_handoff_timeout_call_id is not None:
            self._cancel_handoff_timeout(cancel_handoff_timeout_call_id)
        if cleanup_handoff_agent is not None:
            self._submit_handoff_agent_cleanup_hangup(*cleanup_handoff_agent)
        if handoff_failed_callback is not None:
            self._publish_handoff_failed_callback(*handoff_failed_callback)
        if handoff_connection_failed_callback is not None:
            self._publish_handoff_connection_failed_callback(
                *handoff_connection_failed_callback
            )
        handoff_terminal_callback_sent = (
            handoff_failed_callback is not None
            or handoff_connection_failed_callback is not None
        )
        if self._sync_call_record_terminal(sync_context, sync_status) and (
            not handoff_terminal_callback_sent
        ):
            self._publish_flow_callback(
                sync_context or {},
                status="FAILED",
                message="外呼失败",
                business_id=sync_business_id,
            )

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
        cancel_handoff_timeout = False
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            if record.handoff is not None and record.handoff.state in {
                "waiting_agent",
                "agent_claimed",
                "agent_ringing",
                "bridging",
            }:
                now_ms = _now_ms()
                record.handoff.state = "handoff_failed"
                record.handoff.error = (
                    "customer hangup requested before handoff connected"
                )
                record.handoff.updated_at_ms = now_ms
                cancel_handoff_timeout = True
            self._set_status_locked(record, "hangup_requested")

        if cancel_handoff_timeout:
            self._cancel_handoff_timeout(call_id)
        self._executor.submit(self._run_hangup_worker, call_id, cause)
        return record.to_dict()

    def request_handoff(self, call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        _require_safe_token(call_id, "call_id")
        request = parse_handoff_request(payload)
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            if _is_handoff_inactive_status(record.status):
                raise CallControlError("call is not active", status_code=409)
            if record.handoff is not None and record.handoff.state in {
                "waiting_agent",
                "agent_claimed",
                "agent_ringing",
                "bridging",
                "human_active",
            }:
                raise CallControlError("handoff already in progress", status_code=409)
            now_ms = _now_ms()
            record.handoff = HandoffState(
                state="waiting_agent",
                requested_at_ms=now_ms,
                updated_at_ms=now_ms,
                expires_at_ms=now_ms + request.wait_timeout_seconds * 1000,
                trigger=request.trigger,
                reason=request.reason,
                last_utterance=request.last_utterance,
                summary=request.summary,
                ai_turns=request.ai_turns,
            )
            self._set_status_locked(record, "waiting_agent")
            expires_at_ms = record.handoff.expires_at_ms
            call_payload = record.to_dict()

        if expires_at_ms is not None:
            self._schedule_handoff_timeout(call_id, expires_at_ms)
        return call_payload

    def record_agent_takeover_suggestion(
        self,
        call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        _require_safe_token(call_id, "call_id")
        reason = _optional_str(payload, "reason") or "complaint"
        last_utterance = _optional_str(payload, "last_utterance")
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            if _is_handoff_inactive_status(record.status):
                raise CallControlError("call is not active", status_code=409)
            now_ms = _now_ms()
            if record.agent_takeover_suggestion is None:
                record.agent_takeover_suggestion = AgentTakeoverSuggestion(
                    state="suggested",
                    reason=reason,
                    last_utterance=last_utterance,
                    suggested_at_ms=now_ms,
                    updated_at_ms=now_ms,
                )
            else:
                record.agent_takeover_suggestion.state = "suggested"
                record.agent_takeover_suggestion.reason = reason
                record.agent_takeover_suggestion.last_utterance = last_utterance
                record.agent_takeover_suggestion.updated_at_ms = now_ms
            record.updated_at_ms = now_ms
            return record.to_dict()

    def claim_handoff(self, call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.event_socket.enabled:
            raise CallControlError(
                "FreeSWITCH Event Socket is disabled; handoff claim requires it",
                status_code=503,
            )
        _require_safe_token(call_id, "call_id")
        request = parse_handoff_claim_request(payload)
        expired_error: str | None = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            if _is_handoff_inactive_status(record.status):
                raise CallControlError("call is not active", status_code=409)
            if record.handoff is None:
                raise CallControlError("handoff not requested", status_code=409)
            if record.handoff.state != "waiting_agent":
                raise CallControlError("handoff already claimed", status_code=409)
            now_ms = _now_ms()
            if record.handoff.expires_at_ms is not None and record.handoff.expires_at_ms <= now_ms:
                expired_error = "handoff request expired"
                record.handoff.state = "handoff_failed"
                record.handoff.error = expired_error
                record.handoff.updated_at_ms = now_ms
                self._set_status_locked(record, "handoff_failed")
            else:
                record.handoff.state = "agent_claimed"
                record.handoff.claimed_at_ms = now_ms
                record.handoff.claimed_by = request.claimed_by or request.agent_extension
                record.handoff.agent_extension = request.agent_extension
                record.handoff.agent_uuid = request.agent_uuid
                record.handoff.updated_at_ms = now_ms
                self._set_status_locked(record, "agent_claimed")

        if expired_error is not None:
            self._cancel_handoff_timeout(call_id)
            self._submit_handoff_failure_hangup(
                call_id,
                "handoff_expired_claim_hangup_submit_failed",
            )
            raise CallControlError(expired_error, status_code=409)

        self._cancel_handoff_timeout(call_id)
        try:
            return asyncio.run(self._handoff(call_id, request))
        except (OSError, EOFError, EventSocketError, CallControlError) as err:
            self._release_or_fail_handoff_claim(call_id, str(err))
            if isinstance(err, CallControlError):
                raise
            raise CallControlError(
                f"FreeSWITCH Event Socket request failed: {err}",
                status_code=503,
            ) from err

    def complete_handoff_transcript(
        self,
        call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        _require_safe_token(call_id, "call_id")
        status = _optional_str(payload, "status") or "completed"
        if status not in {"completed", "failed"}:
            raise CallControlError("status must be completed or failed")

        handoff_failed_callback: tuple[dict[str, Any], str | None] | None = None
        rollback_state: dict[str, Any] | None = None
        cleanup_recording_paths: tuple[str | None, str | None] = (None, None)
        human_turns: list[dict[str, Any]] = []
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            if record.handoff is None:
                raise CallControlError("handoff not requested", status_code=409)
            if record.handoff.state == "human_active":
                raise CallControlError(
                    "human handoff is still active",
                    status_code=409,
                )
            if record.handoff.state != "completed":
                raise CallControlError("human handoff is not active", status_code=409)
            if record.handoff.human_transcript_status == "completed":
                return record.to_dict()
            if record.handoff.terminal_callback_status == "FAILED":
                return record.to_dict()

            now_ms = _now_ms()
            record.handoff.human_ended_at_ms = (
                record.handoff.human_ended_at_ms or record.completed_at_ms or now_ms
            )
            record.handoff.updated_at_ms = now_ms
            if status == "failed":
                record.handoff.human_transcript_status = "failed"
                record.handoff.human_transcript_error = (
                    _optional_str(payload, "error") or "human transcript failed"
                )
                record.updated_at_ms = now_ms
                handoff_failed_callback = self._handoff_failed_callback_locked(record)
                call_payload = record.to_dict()
            else:
                human_turns = _normalize_transcript_turns(payload.get("turns"))
                if not human_turns:
                    raise CallControlError(
                        "turns must include at least one human transcript turn"
                    )
                rollback_state = {
                    "human_ended_at_ms": record.handoff.human_ended_at_ms,
                    "updated_at_ms": record.handoff.updated_at_ms,
                    "human_turns": list(record.handoff.human_turns),
                    "human_transcript_status": (
                        record.handoff.human_transcript_status
                    ),
                    "human_transcript_error": record.handoff.human_transcript_error,
                    "record_updated_at_ms": record.updated_at_ms,
                }
                record.handoff.human_turns = human_turns
                record.handoff.human_transcript_status = "completed"
                record.handoff.human_transcript_error = None
                result_payload = {
                    "call_id": record.call_id,
                    "business_id": _business_id(record),
                    "recording_path": record.recording_path,
                    "context": dict(record.context),
                    "turns": [*record.handoff.ai_turns, *record.handoff.human_turns],
                }
                cleanup_recording_paths = (
                    record.handoff.customer_recording_host_path
                    or record.handoff.customer_recording_path,
                    record.handoff.agent_recording_host_path
                    or record.handoff.agent_recording_path,
                )
                call_payload = record.to_dict()

        if status == "failed":
            if handoff_failed_callback is not None:
                self._publish_handoff_failed_callback(*handoff_failed_callback)
            return call_payload

        if self._call_result_writer is None:
            LOGGER.warning(
                "handoff_transcript_completed_without_writer call_id=%s",
                call_id,
            )
            self._cleanup_handoff_recording_files(call_id, cleanup_recording_paths)
            return call_payload
        if not self._call_result_writer.enqueue_nowait(result_payload):
            assert rollback_state is not None
            with self._lock:
                record = self._calls.get(call_id)
                if (
                    record is not None
                    and record.handoff is not None
                    and record.handoff.human_transcript_status == "completed"
                    and record.handoff.human_turns == human_turns
                ):
                    record.handoff.human_ended_at_ms = rollback_state[
                        "human_ended_at_ms"
                    ]
                    record.handoff.updated_at_ms = rollback_state["updated_at_ms"]
                    record.handoff.human_turns = rollback_state["human_turns"]
                    record.handoff.human_transcript_status = rollback_state[
                        "human_transcript_status"
                    ]
                    record.handoff.human_transcript_error = rollback_state[
                        "human_transcript_error"
                    ]
                    record.updated_at_ms = rollback_state["record_updated_at_ms"]
            raise CallControlError("call result writer queue is full", status_code=503)
        self._cleanup_handoff_recording_files(call_id, cleanup_recording_paths)
        return call_payload

    def _cleanup_handoff_recording_files(
        self,
        call_id: str,
        paths: tuple[str | None, str | None],
    ) -> None:
        for path in paths:
            if not path:
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError:
                LOGGER.warning(
                    "handoff_recording_cleanup_failed call_id=%s path=%s",
                    call_id,
                    path,
                    exc_info=True,
                )

    def _build_record(self, request: CreateCallRequest) -> OutboundCallRecord:
        outbound = self.config.outbound
        destination = request.destination
        assert destination is not None
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
        recording_path = build_call_recording_path(
            self.config.call_recording,
            media_call_id=call_id,
            external_call_id=request.external_call_id,
            context=request.context,
        )

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
            recording_path=recording_path,
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
                failed_business_id = _business_id(record)
            if self._sync_call_record_failed(failed_context):
                self._publish_flow_callback(
                    failed_context,
                    status="FAILED",
                    message="外呼失败",
                    business_id=failed_business_id,
                )
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
                failed_business_id = _business_id(record)
            else:
                if record.status in {"originating", "queued"}:
                    self._set_status_locked(record, "originated")
            record.originate_completed_at_ms = _now_ms()
        if self._sync_call_record_failed(failed_context):
            self._publish_flow_callback(
                failed_context or {},
                status="FAILED",
                message="外呼失败",
                business_id=failed_business_id,
            )
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

    def _run_stop_handoff_recording_worker(self, call_id: str) -> None:
        try:
            asyncio.run(self._stop_handoff_recording(call_id))
        except Exception:
            LOGGER.exception("handoff_recording_stop_worker_failed call_id=%s", call_id)
            self._mark_handoff_recording_failed(
                call_id,
                "internal handoff recording stop worker error",
            )

    def _schedule_handoff_timeout(self, call_id: str, expires_at_ms: int) -> None:
        delay_seconds = max(0.0, (expires_at_ms - _now_ms()) / 1000)
        timer = threading.Timer(
            delay_seconds,
            self._expire_handoff_request,
            args=(call_id, expires_at_ms),
        )
        timer.daemon = True
        with self._lock:
            previous = self._handoff_timeout_timers.pop(call_id, None)
            self._handoff_timeout_timers[call_id] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _cancel_handoff_timeout(self, call_id: str) -> None:
        with self._lock:
            timer = self._handoff_timeout_timers.pop(call_id, None)
        if timer is not None:
            timer.cancel()

    def _expire_handoff_request(self, call_id: str, expires_at_ms: int) -> None:
        should_play_notice_and_hangup = False
        with self._lock:
            self._handoff_timeout_timers.pop(call_id, None)
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            handoff = record.handoff
            if handoff.state != "waiting_agent":
                return
            if handoff.expires_at_ms != expires_at_ms:
                return
            if _is_terminal_status(record.status):
                return
            now_ms = _now_ms()
            if expires_at_ms > now_ms:
                self._schedule_handoff_timeout(call_id, expires_at_ms)
                return
            handoff.state = "handoff_failed"
            handoff.error = "handoff request expired"
            handoff.updated_at_ms = now_ms
            self._set_status_locked(record, "handoff_failed")
            should_play_notice_and_hangup = True

        if should_play_notice_and_hangup:
            self._submit_handoff_failure_notice_then_hangup(
                call_id,
                "handoff_timeout_hangup_submit_failed",
            )

    def _submit_handoff_failure_notice_then_hangup(
        self,
        call_id: str,
        log_event: str,
    ) -> None:
        try:
            self._executor.submit(
                self._run_handoff_failure_notice_then_hangup_worker,
                call_id,
            )
        except RuntimeError:
            LOGGER.warning("%s call_id=%s", log_event, call_id, exc_info=True)

    def _run_handoff_failure_notice_then_hangup_worker(self, call_id: str) -> None:
        try:
            asyncio.run(self._play_handoff_failure_notice_then_hangup(call_id))
        except Exception:
            LOGGER.warning(
                "handoff_failure_notice_worker_failed call_id=%s",
                call_id,
                exc_info=True,
            )
            self._submit_handoff_failure_hangup(
                call_id,
                "handoff_failure_notice_fallback_hangup_submit_failed",
            )

    async def _play_handoff_failure_notice_then_hangup(self, call_id: str) -> None:
        notice = self._prepare_handoff_notice_audio(
            call_id,
            HANDOFF_AGENT_BUSY_PROMPT_TEXT,
        )
        if notice is not None:
            path, duration_seconds = notice
            try:
                reply = (await self._dialer_factory().play_file(call_id, path)).strip()
            except Exception:
                LOGGER.warning(
                    "handoff_failure_notice_play_failed call_id=%s path=%s",
                    call_id,
                    path,
                    exc_info=True,
                )
            else:
                if reply.startswith("-ERR"):
                    LOGGER.warning(
                        "handoff_failure_notice_play_rejected call_id=%s path=%s "
                        "reply=%s",
                        call_id,
                        path,
                        reply,
                    )
                else:
                    await asyncio.sleep(duration_seconds)
        await self._hangup(call_id, cause="NORMAL_CLEARING")

    def _prepare_handoff_notice_audio(
        self,
        call_id: str,
        text: str,
    ) -> tuple[str, float] | None:
        generator = self._opening_generator
        if generator is None:
            return None

        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return None
            voice = record.opening.voice if record.opening is not None else "female"
            speaker = (
                record.opening.speaker
                if record.opening is not None
                else self.config.doubao_s2s.speaker
            )

        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        opening = OpeningRequest(
            voice=voice,
            speaker=speaker,
            business={},
            opening_text=text,
            opening_text_hash=text_hash,
        )
        try:
            audio = generator.generate(opening)
            prepared = build_prepared_opening_audio(
                call_id=f"{call_id}-handoff-notice",
                opening=opening,
                audio=audio,
                config=self.config,
            )
        except (OpeningGenerationFailed, OSError, ValueError):
            LOGGER.warning(
                "handoff_failure_notice_generation_failed call_id=%s",
                call_id,
                exc_info=True,
            )
            return None

        filename = f"{text_hash}.wav"
        fs_base = self.config.call_recording.directory.rstrip("/")
        host_base = (
            self.config.call_recording.host_directory.rstrip("/")
            or self.config.call_recording.directory.rstrip("/")
        )
        fs_path = f"{fs_base}/handoff-prompts/{filename}"
        host_path = Path(host_base) / "handoff-prompts" / filename
        try:
            write_pcm16_wav(
                host_path,
                b"".join(prepared.phone_frames),
                sample_rate=self.config.freeswitch.sample_rate,
                channels=self.config.freeswitch.channels,
            )
        except OSError:
            LOGGER.warning(
                "handoff_failure_notice_wav_write_failed call_id=%s path=%s",
                call_id,
                host_path,
                exc_info=True,
            )
            return None
        duration_seconds = (
            len(prepared.phone_frames) * self.config.freeswitch.frame_duration_ms
        ) / 1000
        return fs_path, max(duration_seconds, 0.1)

    def _submit_handoff_failure_hangup(self, call_id: str, log_event: str) -> None:
        try:
            self._executor.submit(
                self._run_hangup_worker,
                call_id,
                "NORMAL_CLEARING",
            )
        except RuntimeError:
            LOGGER.warning("%s call_id=%s", log_event, call_id, exc_info=True)

    def _submit_handoff_agent_cleanup_hangup(
        self,
        call_id: str,
        agent_uuid: str,
    ) -> None:
        try:
            self._executor.submit(
                self._run_handoff_agent_cleanup_hangup_worker,
                call_id,
                agent_uuid,
            )
        except RuntimeError:
            LOGGER.warning(
                "handoff_agent_cleanup_submit_failed call_id=%s agent_uuid=%s",
                call_id,
                agent_uuid,
                exc_info=True,
            )

    def _run_handoff_agent_cleanup_hangup_worker(
        self,
        call_id: str,
        agent_uuid: str,
    ) -> None:
        try:
            asyncio.run(
                self._dialer_factory().hangup(
                    agent_uuid,
                    cause="NORMAL_CLEARING",
                )
            )
        except Exception:
            LOGGER.warning(
                "handoff_agent_cleanup_failed call_id=%s agent_uuid=%s",
                call_id,
                agent_uuid,
                exc_info=True,
            )

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
                failed_business_id = _business_id(record)
            else:
                self._set_status_locked(record, "hangup_sent")
        if self._sync_call_record_failed(failed_context):
            self._publish_flow_callback(
                failed_context or {},
                status="FAILED",
                message="外呼失败",
                business_id=failed_business_id,
            )

    async def _stop_handoff_recording(self, call_id: str) -> None:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            handoff = record.handoff
            customer_path = handoff.customer_recording_path
            agent_path = handoff.agent_recording_path
            agent_uuid = handoff.agent_uuid

        if not customer_path or not agent_path or not agent_uuid:
            self._mark_handoff_recording_failed(call_id, "recording path is missing")
            return

        dialer = self._dialer_factory()
        errors = []
        for channel_uuid, path in ((call_id, customer_path), (agent_uuid, agent_path)):
            try:
                reply = (await dialer.stop_recording(channel_uuid, path)).strip()
            except (OSError, EOFError, EventSocketError) as err:
                errors.append(str(err))
                continue
            if reply.startswith("-ERR") and not _recording_stop_reply_is_terminal_ok(
                reply
            ):
                errors.append(reply)

        should_process_transcript = False
        handoff_failed_callback: tuple[dict[str, Any], str | None] | None = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            now_ms = _now_ms()
            record.handoff.recording_stopped_at_ms = now_ms
            record.handoff.updated_at_ms = now_ms
            if errors:
                recording_error = "; ".join(errors)
                record.handoff.recording_status = "failed"
                record.handoff.recording_error = recording_error
                if record.handoff.human_transcript_status == "pending":
                    record.handoff.human_transcript_status = "failed"
                    record.handoff.human_transcript_error = (
                        f"recording failed: {recording_error}"
                    )
                handoff_failed_callback = self._handoff_failed_callback_locked(record)
            else:
                record.handoff.recording_status = "completed"
                record.handoff.recording_error = None
                should_process_transcript = True

        if handoff_failed_callback is not None:
            self._publish_handoff_failed_callback(*handoff_failed_callback)
        if should_process_transcript:
            self._maybe_submit_handoff_transcript_processor(call_id)

    async def _handoff(self, call_id: str, request: HandoffClaimRequest) -> dict[str, Any]:
        dialer = self._dialer_factory()
        endpoint = await dialer.resolve_endpoint(
            f"sofia_contact:*/{request.agent_extension}"
        )
        inactive_error = self._handoff_customer_inactive_error(call_id)
        if inactive_error is not None:
            raise CallControlError(inactive_error, status_code=409)
        with self._lock:
            record = self._calls[call_id]
            assert record.handoff is not None
            now_ms = _now_ms()
            record.handoff.state = "agent_ringing"
            record.handoff.agent_endpoint = endpoint
            record.handoff.updated_at_ms = now_ms
            self._set_status_locked(record, "agent_ringing")

        command = build_webrtc_agent_originate_command(
            agent_uuid=request.agent_uuid,
            endpoint=endpoint,
            timeout_seconds=request.timeout_seconds,
        )
        originate_reply = (await dialer.originate(command)).strip()
        if originate_reply.startswith("-ERR"):
            raise CallControlError(originate_reply, status_code=503)
        inactive_error = self._handoff_customer_inactive_error(call_id)
        if inactive_error is not None:
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise CallControlError(inactive_error, status_code=409)

        with self._lock:
            record = self._calls[call_id]
            assert record.handoff is not None
            now_ms = _now_ms()
            record.handoff.state = "bridging"
            record.handoff.agent_originate_reply = originate_reply
            record.handoff.answered_at_ms = now_ms
            record.handoff.updated_at_ms = now_ms
            self._set_status_locked(record, "bridging")

        try:
            break_reply = (await dialer.break_audio_stream(call_id)).strip()
        except Exception:
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise
        inactive_error = self._handoff_customer_inactive_error(call_id)
        if inactive_error is not None:
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise CallControlError(inactive_error, status_code=409)
        with self._lock:
            record = self._calls[call_id]
            assert record.handoff is not None
            record.handoff.audio_stream_break_reply = break_reply
            record.handoff.updated_at_ms = _now_ms()

        try:
            bridge_reply = (await dialer.bridge(call_id, request.agent_uuid)).strip()
        except Exception:
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise
        if bridge_reply.startswith("-ERR"):
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise CallControlError(bridge_reply, status_code=503)
        inactive_error = self._handoff_customer_inactive_error(call_id)
        if inactive_error is not None:
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise CallControlError(inactive_error, status_code=409)

        recording_status = "disabled"
        recording_error = None
        customer_recording_path = None
        agent_recording_path = None
        customer_recording_host_path = None
        agent_recording_host_path = None
        recording_started_at_ms = None
        if self.config.features.recording_enabled:
            recording_status = "recording"
            recording_started_at_ms = _now_ms()
            try:
                (
                    customer_recording_path,
                    agent_recording_path,
                    customer_recording_host_path,
                    agent_recording_host_path,
                ) = _handoff_recording_paths(
                    self.config.features.recording_dir,
                    self.config.features.recording_host_dir,
                    customer_call_id=call_id,
                    agent_uuid=request.agent_uuid,
                )
                for channel_uuid, path in (
                    (call_id, customer_recording_path),
                    (request.agent_uuid, agent_recording_path),
                ):
                    reply = (
                        await dialer.start_recording(
                            channel_uuid,
                            path,
                            read_only=True,
                        )
                    ).strip()
                    if reply.startswith("-ERR"):
                        raise CallControlError(reply, status_code=503)
            except Exception as err:
                recording_status = "failed"
                recording_error = str(err)

        inactive_error = self._handoff_customer_inactive_error(call_id)
        if inactive_error is not None:
            await self._cleanup_aborted_agent_call(
                dialer,
                call_id=call_id,
                agent_uuid=request.agent_uuid,
            )
            raise CallControlError(inactive_error, status_code=409)

        with self._lock:
            record = self._calls[call_id]
            assert record.handoff is not None
            now_ms = _now_ms()
            record.handoff.state = "human_active"
            record.handoff.audio_stream_break_reply = break_reply
            record.handoff.bridge_reply = bridge_reply
            record.handoff.bridged_at_ms = now_ms
            record.handoff.human_transcript_status = (
                record.handoff.human_transcript_status or "pending"
            )
            if (
                recording_status == "failed"
                and record.handoff.human_transcript_status == "pending"
            ):
                record.handoff.human_transcript_status = "failed"
                record.handoff.human_transcript_error = (
                    f"recording failed: {recording_error}"
                )
            record.handoff.recording_status = recording_status
            record.handoff.recording_error = recording_error
            record.handoff.customer_recording_path = customer_recording_path
            record.handoff.agent_recording_path = agent_recording_path
            record.handoff.customer_recording_host_path = customer_recording_host_path
            record.handoff.agent_recording_host_path = agent_recording_host_path
            record.handoff.recording_started_at_ms = recording_started_at_ms
            record.handoff.updated_at_ms = now_ms
            self._set_status_locked(record, "human_active")
            return record.to_dict()

    def _handoff_customer_inactive_error(self, call_id: str) -> str | None:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                return "call not found"
            if _is_handoff_inactive_status(record.status):
                return "customer call ended before handoff connected"
            if record.handoff is None:
                return "handoff not requested"
            if record.handoff.state == "handoff_failed":
                return record.handoff.error or "handoff failed"
            if record.handoff.state not in {
                "agent_claimed",
                "agent_ringing",
                "bridging",
            }:
                return f"handoff is no longer active: {record.handoff.state}"
        return None

    async def _cleanup_aborted_agent_call(
        self,
        dialer: Any,
        *,
        call_id: str,
        agent_uuid: str,
    ) -> None:
        try:
            await dialer.hangup(agent_uuid, cause="NORMAL_CLEARING")
        except Exception:
            LOGGER.warning(
                "handoff_agent_cleanup_failed call_id=%s agent_uuid=%s",
                call_id,
                agent_uuid,
                exc_info=True,
            )

    def _mark_handoff_failed(self, call_id: str, error: str) -> None:
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            record.handoff.state = "handoff_failed"
            record.handoff.error = error
            record.handoff.updated_at_ms = _now_ms()
            self._set_status_locked(record, "handoff_failed")

    def _release_or_fail_handoff_claim(self, call_id: str, error: str) -> None:
        reschedule_expires_at_ms: int | None = None
        should_play_notice_and_hangup = False
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            handoff = record.handoff
            now_ms = _now_ms()
            if _is_handoff_inactive_status(record.status):
                handoff.state = "handoff_failed"
                handoff.error = error
                handoff.updated_at_ms = now_ms
                record.updated_at_ms = now_ms
                return
            if (
                handoff.expires_at_ms is not None
                and handoff.expires_at_ms > now_ms
                and not _is_handoff_inactive_status(record.status)
            ):
                handoff.state = "waiting_agent"
                handoff.claimed_at_ms = None
                handoff.claimed_by = None
                handoff.agent_extension = None
                handoff.agent_uuid = None
                handoff.agent_endpoint = None
                handoff.answered_at_ms = None
                handoff.bridged_at_ms = None
                handoff.agent_originate_reply = None
                handoff.audio_stream_break_reply = None
                handoff.bridge_reply = None
                handoff.error = error
                handoff.updated_at_ms = now_ms
                self._set_status_locked(record, "waiting_agent")
                reschedule_expires_at_ms = handoff.expires_at_ms
            else:
                handoff.state = "handoff_failed"
                handoff.error = error
                handoff.updated_at_ms = now_ms
                self._set_status_locked(record, "handoff_failed")
                should_play_notice_and_hangup = not _is_terminal_status(record.status)

        if reschedule_expires_at_ms is not None:
            self._schedule_handoff_timeout(call_id, reschedule_expires_at_ms)
        if should_play_notice_and_hangup:
            self._submit_handoff_failure_notice_then_hangup(
                call_id,
                "handoff_failed_hangup_submit_failed",
            )

    def _mark_handoff_recording_failed(self, call_id: str, error: str) -> None:
        handoff_failed_callback: tuple[dict[str, Any], str | None] | None = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            record.handoff.recording_status = "failed"
            record.handoff.recording_error = error
            if record.handoff.human_transcript_status == "pending":
                record.handoff.human_transcript_status = "failed"
                record.handoff.human_transcript_error = f"recording failed: {error}"
            record.handoff.updated_at_ms = _now_ms()
            record.updated_at_ms = record.handoff.updated_at_ms
            handoff_failed_callback = self._handoff_failed_callback_locked(record)
        if handoff_failed_callback is not None:
            self._publish_handoff_failed_callback(*handoff_failed_callback)

    def _maybe_submit_handoff_transcript_processor(self, call_id: str) -> None:
        if self._handoff_transcript_processor is None:
            return

        handoff_failed_callback: tuple[dict[str, Any], str | None] | None = None
        job: dict[str, Any] | None = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            handoff = record.handoff
            if (
                handoff.state != "completed"
                or handoff.human_transcript_status != "pending"
                or handoff.recording_status != "completed"
            ):
                return
            customer_recording_path = (
                handoff.customer_recording_host_path
                or handoff.customer_recording_path
            )
            agent_recording_path = (
                handoff.agent_recording_host_path or handoff.agent_recording_path
            )
            if (
                not handoff.agent_uuid
                or not customer_recording_path
                or not agent_recording_path
            ):
                handoff.human_transcript_status = "failed"
                handoff.human_transcript_error = "recording path is missing"
                handoff.updated_at_ms = _now_ms()
                record.updated_at_ms = handoff.updated_at_ms
                handoff_failed_callback = self._handoff_failed_callback_locked(record)
            else:
                agent_id = (
                    handoff.claimed_by or handoff.agent_extension or handoff.agent_uuid
                )
                job = {
                    "call_id": record.call_id,
                    "context": dict(record.context),
                    "agent_id": agent_id,
                    "agent_uuid": handoff.agent_uuid,
                    "customer_recording_path": customer_recording_path,
                    "agent_recording_path": agent_recording_path,
                }
                handoff.human_transcript_status = "processing"
                handoff.human_transcript_error = None
                handoff.updated_at_ms = _now_ms()
                record.updated_at_ms = handoff.updated_at_ms

        if handoff_failed_callback is not None:
            self._publish_handoff_failed_callback(*handoff_failed_callback)
        if job is None:
            return

        self._executor.submit(
            self._run_handoff_transcript_processor_worker,
            call_id,
            job,
        )

    def _run_handoff_transcript_processor_worker(
        self,
        call_id: str,
        job: dict[str, Any],
    ) -> None:
        assert self._handoff_transcript_processor is not None
        try:
            turns = self._handoff_transcript_processor.process(job)
            self.complete_handoff_transcript(call_id, {"turns": turns})
        except Exception as err:
            LOGGER.warning(
                "handoff_transcript_processor_failed call_id=%s error=%s",
                call_id,
                err,
                exc_info=True,
            )
            self._mark_handoff_transcript_failed(call_id, str(err))

    def _mark_handoff_transcript_failed(self, call_id: str, error: str) -> None:
        handoff_failed_callback: tuple[dict[str, Any], str | None] | None = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None or record.handoff is None:
                return
            record.handoff.human_transcript_status = "failed"
            record.handoff.human_transcript_error = error
            record.handoff.updated_at_ms = _now_ms()
            record.updated_at_ms = record.handoff.updated_at_ms
            handoff_failed_callback = self._handoff_failed_callback_locked(record)
        if handoff_failed_callback is not None:
            self._publish_handoff_failed_callback(*handoff_failed_callback)

    def _handoff_failed_callback_locked(
        self,
        record: OutboundCallRecord,
    ) -> tuple[dict[str, Any], str | None] | None:
        handoff = record.handoff
        if handoff is None:
            return None
        if handoff.state != "completed":
            return None
        if handoff.human_transcript_status != "failed":
            return None
        if handoff.terminal_callback_status is not None:
            return None
        handoff.terminal_callback_status = "FAILED"
        handoff.updated_at_ms = _now_ms()
        record.updated_at_ms = handoff.updated_at_ms
        return dict(record.context), _business_id(record)

    def _handoff_connection_failed_callback_locked(
        self,
        record: OutboundCallRecord,
    ) -> tuple[dict[str, Any], str | None] | None:
        handoff = record.handoff
        if handoff is None:
            return None
        if handoff.state != "handoff_failed":
            return None
        if not _is_terminal_status(record.status):
            return None
        if handoff.terminal_callback_status is not None:
            return None
        handoff.terminal_callback_status = "FAILED"
        handoff.updated_at_ms = _now_ms()
        record.updated_at_ms = handoff.updated_at_ms
        return dict(record.context), _business_id(record)

    def _publish_handoff_failed_callback(
        self,
        context: dict[str, Any],
        business_id: str | None,
    ) -> None:
        self._publish_flow_callback(
            context,
            status="FAILED",
            message="人工转写失败",
            business_id=business_id,
        )

    def _publish_handoff_connection_failed_callback(
        self,
        context: dict[str, Any],
        business_id: str | None,
    ) -> None:
        self._publish_flow_callback(
            context,
            status="FAILED",
            message="转人工失败",
            business_id=business_id,
        )

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
            failed_business_id = _business_id(record)
        if self._sync_call_record_failed(failed_context):
            self._publish_flow_callback(
                failed_context,
                status="FAILED",
                message="外呼失败",
                business_id=failed_business_id,
            )

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
            if record.handoff is not None and record.handoff.state == "human_active":
                record.handoff.state = "completed"
                record.handoff.human_ended_at_ms = (
                    record.handoff.human_ended_at_ms or now_ms
                )
                record.handoff.updated_at_ms = now_ms
            elif record.handoff is not None and record.handoff.state in {
                "waiting_agent",
                "agent_claimed",
                "agent_ringing",
                "bridging",
            }:
                record.handoff.state = "handoff_failed"
                record.handoff.error = "customer hung up before handoff connected"
                record.handoff.updated_at_ms = now_ms
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
            if record.external_call_id:
                self._external_call_index.pop(record.external_call_id, None)
            task_id = _context_text(record.context.get("taskId"))
            if task_id:
                self._external_call_index.pop(task_id, None)
            self._discard_opening_locked(record.call_id)

    def _record_for_idempotency_key_locked(
        self,
        idempotency_key: str,
    ) -> OutboundCallRecord | None:
        call_id = self._external_call_index.get(idempotency_key)
        if call_id is None:
            return None
        return self._calls.get(call_id)

    def _discard_opening_locked(self, call_id: str) -> None:
        if self._opening_store is not None:
            self._opening_store.discard(call_id)

    def _sync_call_record_started(self, context: dict[str, Any] | None) -> bool:
        if self._call_record_updater is None or context is None:
            return False
        try:
            return bool(self._call_record_updater.mark_started(context))
        except Exception:
            LOGGER.warning("call_record_started_sync_failed", exc_info=True)
            return False

    def _sync_call_record_failed(self, context: dict[str, Any] | None) -> bool:
        if self._call_record_updater is None or context is None:
            return False
        try:
            return bool(self._call_record_updater.mark_failed(context))
        except Exception:
            LOGGER.warning("call_record_failed_sync_failed", exc_info=True)
            return False

    def _sync_call_record_no_answer(self, context: dict[str, Any] | None) -> bool:
        if self._call_record_updater is None or context is None:
            return False
        try:
            return bool(self._call_record_updater.mark_no_answer(context))
        except Exception:
            LOGGER.warning("call_record_no_answer_sync_failed", exc_info=True)
            return False

    def _sync_call_record_terminal(
        self,
        context: dict[str, Any] | None,
        status: str | None,
    ) -> bool:
        if status == "no_answer":
            return self._sync_call_record_no_answer(context)
        if status in {"failed", "busy", "canceled", "hangup_failed"}:
            return self._sync_call_record_failed(context)
        return False

    def _publish_flow_callback(
        self,
        context: dict[str, Any],
        *,
        status: str,
        message: str,
        business_id: str | None,
    ) -> None:
        if self._flow_callback_writer is None:
            return
        try:
            event = build_flow_callback_event(
                context,
                status=status,
                message=message,
                business_id=business_id,
            )
            if event is not None:
                self._flow_callback_writer.publish(event)
        except Exception:
            LOGGER.warning("flow_callback_publish_failed status=%s", status, exc_info=True)

    def _validate_flow_callback_context(self, context: dict[str, Any]) -> None:
        if not self.config.flow_callback.enabled:
            return
        if _context_text(context.get("taskId")) is None:
            raise CallControlError(
                "context.taskId is required when flow callback is enabled"
            )

    def _validate_flow_callback_runtime_ready(self) -> None:
        if not self.config.flow_callback.enabled:
            return
        if self._flow_callback_writer is None:
            raise CallControlError(
                "flow callback writer is unavailable",
                status_code=503,
            )
        if self._call_record_updater is None or self._call_result_writer is None:
            raise CallControlError(
                "flow callback requires call_record persistence",
                status_code=503,
            )

    def _resolve_call_destination(
        self,
        request: CreateCallRequest,
    ) -> CreateCallRequest:
        if request.destination is not None:
            return request
        if self._destination_resolver is None:
            status_code = 503 if _context_text(request.context.get("debtId")) else 400
            raise CallControlError(
                "destination is required when debt phone resolver is unavailable",
                status_code=status_code,
            )
        try:
            destination = self._destination_resolver.resolve(request.context)
        except Exception as err:
            raise CallControlError(
                "failed to resolve destination from debtId",
                status_code=503,
            ) from err
        destination_text = _context_text(destination)
        if destination_text is None:
            raise CallControlError("debtor phone not found", status_code=400)
        _require_safe_token(destination_text, "destination")
        return replace(request, destination=destination_text)


def parse_create_call_request(payload: dict[str, Any]) -> CreateCallRequest:
    if not isinstance(payload, dict):
        raise CallControlError("request body must be a JSON object")

    _validate_node_code(payload)
    context = _normalized_context(payload)
    _validate_no_local_placeholder_business_ids(context)
    destination = _optional_str(payload, "destination")
    if destination is not None:
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

    try:
        opening = parse_opening_request(payload.get("opening"))
    except OpeningGenerationFailed as err:
        raise CallControlError(str(err)) from err

    call_id = _optional_safe_str(payload, "callId") or _optional_safe_str(
        context,
        "callId",
    )
    external_call_id = (
        _optional_safe_str(payload, "external_call_id")
        or _optional_safe_str(payload, "businessId")
        or call_id
    )
    return CreateCallRequest(
        destination=destination,
        external_call_id=external_call_id,
        endpoint=endpoint,
        dialplan_extension=dialplan_extension,
        dialplan_context=dialplan_context,
        caller_id_name=_optional_safe_str(payload, "caller_id_name"),
        caller_id_number=_optional_safe_str(payload, "caller_id_number"),
        originate_timeout_seconds=timeout,
        context=context,
        opening=opening,
    )


def parse_handoff_request(payload: dict[str, Any]) -> HandoffRequest:
    if not isinstance(payload, dict):
        raise CallControlError("request body must be a JSON object")
    wait_timeout_seconds = _optional_int(payload, "wait_timeout_seconds")
    if wait_timeout_seconds is None:
        wait_timeout_seconds = 60
    if not 1 <= wait_timeout_seconds <= 300:
        raise CallControlError("wait_timeout_seconds must be between 1 and 300")
    return HandoffRequest(
        trigger=_optional_str(payload, "trigger"),
        reason=_optional_str(payload, "reason"),
        last_utterance=_optional_str(payload, "last_utterance"),
        summary=_optional_str(payload, "summary"),
        ai_turns=_normalize_transcript_turns(payload.get("ai_turns") or payload.get("turns")),
        wait_timeout_seconds=wait_timeout_seconds,
    )


def parse_handoff_claim_request(payload: dict[str, Any]) -> HandoffClaimRequest:
    if not isinstance(payload, dict):
        raise CallControlError("request body must be a JSON object")
    agent_extension = _optional_safe_str(payload, "agent_extension") or "1001"
    agent_uuid = _optional_safe_str(payload, "agent_uuid") or uuid.uuid4().hex
    timeout_seconds = _optional_int(payload, "timeout_seconds")
    if timeout_seconds is None:
        timeout_seconds = 20
    if not 1 <= timeout_seconds <= 120:
        raise CallControlError("timeout_seconds must be between 1 and 120")
    return HandoffClaimRequest(
        agent_extension=agent_extension,
        agent_uuid=agent_uuid,
        timeout_seconds=timeout_seconds,
        claimed_by=_optional_safe_str(payload, "claimed_by"),
    )


def _normalize_transcript_turns(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    turns: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = _optional_str(item, "role")
        text = _optional_str(item, "text")
        if role not in {"assistant", "user"} or text is None:
            continue
        turn: dict[str, Any] = {"role": role, "text": text}
        speaker_type = _optional_str(item, "speaker_type")
        if speaker_type is not None:
            turn["speaker_type"] = speaker_type
        agent_id = _optional_safe_str(item, "agent_id")
        if agent_id is not None:
            turn["agent_id"] = agent_id
        start_ms = _optional_int_value(item.get("start_ms"))
        if start_ms is not None:
            turn["start_ms"] = start_ms
        end_ms = _optional_int_value(item.get("end_ms"))
        if end_ms is not None:
            turn["end_ms"] = end_ms
        confidence = _optional_float_value(item.get("confidence"))
        if confidence is not None:
            turn["confidence"] = confidence
        turns.append(turn)
    return turns


def _normalized_context(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("context", {})
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise CallControlError("context must be a JSON object")
    normalized = dict(context)
    for key in (
        "tenantId",
        "taskId",
        "callId",
        "businessId",
        "nodeCode",
        "identityName",
        "debtId",
    ):
        if key not in normalized and payload.get(key) is not None:
            normalized[key] = payload[key]
    return normalized


def _validate_node_code(payload: dict[str, Any]) -> None:
    node_code = _optional_str(payload, "nodeCode")
    if node_code is not None and node_code != "ai_call":
        raise CallControlError("nodeCode must be ai_call")


def _validate_no_local_placeholder_business_ids(context: dict[str, Any]) -> None:
    for key in ("callId", "taskId", "businessId"):
        value = _optional_str(context, key)
        if value is not None and value.startswith(LOCAL_PLACEHOLDER_BUSINESS_ID_PREFIX):
            raise CallControlError(
                f"{key} must reference a real call_record; local placeholder "
                "business IDs are not allowed",
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
    if record.recording_path:
        variables["sip_realtime_recording_path"] = record.recording_path

    return (
        f"originate {_format_originate_variables(variables)}{record.endpoint} "
        f"{record.dialplan_extension} XML {record.dialplan_context}"
    )


def build_call_recording_path(
    config: CallRecordingConfig,
    *,
    media_call_id: str,
    external_call_id: str | None,
    context: dict[str, Any],
) -> str | None:
    if not config.enabled:
        return None

    business_call_id = (
        _context_text(context.get("callId"))
        or _context_text(external_call_id)
        or media_call_id
    )
    _require_safe_token(business_call_id, "recording_call_id")
    directory = config.directory.rstrip("/")
    _require_safe_token(directory, "call_recording.directory")
    return f"{directory}/{business_call_id}.wav"


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
    if status == "human_active":
        return "human_active"
    if status == "waiting_agent":
        return "waiting_agent"
    if status == "agent_claimed":
        return "agent_claimed"
    if status == "agent_ringing":
        return "agent_ringing"
    if status == "bridging":
        return "bridging"
    if status == "handoff_failed":
        return "handoff_failed"
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
        "human_active": "人工通话中",
        "waiting_agent": "等待人工接听",
        "agent_claimed": "坐席已抢接",
        "agent_ringing": "呼叫坐席中",
        "bridging": "桥接中",
        "handoff_failed": "转人工失败",
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


def _business_id(record: OutboundCallRecord) -> str:
    return record.external_call_id or record.call_id


def _handoff_summary(handoff: HandoffState | None) -> str | None:
    if handoff is None:
        return None
    if handoff.summary:
        return handoff.summary
    if handoff.last_utterance:
        return f"客户要求转人工：{handoff.last_utterance}"
    if handoff.state:
        return "客户要求转人工"
    return None


def _idempotency_key(request: CreateCallRequest) -> str | None:
    return request.external_call_id or _context_text(request.context.get("taskId"))


def _context_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_terminal_status(status: str) -> bool:
    return status in {
        "completed",
        "failed",
        "busy",
        "no_answer",
        "canceled",
        "hangup_failed",
    }


def _is_handoff_inactive_status(status: str) -> bool:
    return _is_terminal_status(status) or status in {
        "hangup_requested",
        "hangup_sent",
    }


def _can_takeover_from_suggestion(record: OutboundCallRecord) -> bool:
    if record.agent_takeover_suggestion is None:
        return False
    if record.handoff is not None:
        return False
    return not _is_handoff_inactive_status(record.status)


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


def _optional_int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _require_safe_token(value: str, name: str) -> None:
    if not SAFE_TOKEN_RE.match(value):
        raise CallControlError(f"{name} contains unsupported characters")


def _now_ms() -> int:
    return int(time.time() * 1000)
