from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import json
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.legacy.client import connect
from websockets.legacy.exceptions import InvalidStatusCode

from .audio_codec import (
    float32le_to_pcm_s16le,
    pcm_s16le_frame_bytes,
    split_audio_frames,
)
from .realtime_types import RealtimeDialogConfig

DEFAULT_WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
DEFAULT_RESOURCE_ID = "volc.speech.dialog"
DEFAULT_REALTIME_APP_KEY = "PlgvMymc7f3tQnJ6"
DEFAULT_SPEAKER = "zh_female_vv_jupiter_bigtts"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_OUTPUT_SAMPLE_RATE = 24000
DEFAULT_BITS = 16
DEFAULT_CHANNELS = 1
DEFAULT_LANGUAGE = "zh-CN"

VERSION_1 = 0x1
HEADER_SIZE_UNITS = 0x1

MESSAGE_TYPE_FULL_CLIENT = 0x1
MESSAGE_TYPE_AUDIO_ONLY_CLIENT = 0x2
MESSAGE_TYPE_FULL_SERVER = 0x9
MESSAGE_TYPE_AUDIO_ONLY_SERVER = 0xB
MESSAGE_TYPE_ERROR = 0xF

FLAG_NO_SEQUENCE = 0x0
FLAG_POSITIVE_SEQUENCE = 0x1
FLAG_NEGATIVE_SEQUENCE = 0x2
FLAG_NEGATIVE_WITH_SEQUENCE = 0x3
FLAG_WITH_EVENT = 0x4

SERIALIZATION_NONE = 0x0
SERIALIZATION_JSON = 0x1

COMPRESSION_NONE = 0x0
COMPRESSION_GZIP = 0x1

EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_ENDED = 52

EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_USAGE_RESPONSE = 154

EVENT_TASK_AUDIO = 200
EVENT_SAY_HELLO = 300
EVENT_TTS_STARTED = 350
EVENT_TTS_SEGMENT_END = 351
EVENT_TTS_AUDIO_DATA = 352
EVENT_TTS_FINISHED = 359
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_TTS_TEXT = 500
EVENT_USER_TEXT = 501
EVENT_CLIENT_INTERRUPT = 515
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559

TERMINAL_EVENTS = {
    EVENT_TTS_FINISHED,
    EVENT_SESSION_FINISHED,
}
ERROR_EVENTS = {
    EVENT_CONNECTION_FAILED,
    EVENT_SESSION_FAILED,
}


class DoubaoS2SError(RuntimeError):
    pass


@dataclass(frozen=True)
class DoubaoS2SCredentials:
    app_id: str
    access_token: str
    app_key: str = DEFAULT_REALTIME_APP_KEY
    resource_id: str = DEFAULT_RESOURCE_ID
    websocket_url: str = DEFAULT_WS_URL
    connect_id: str | None = None

    def validate(self) -> None:
        missing = []
        if not self.app_id:
            missing.append("app_id")
        if not self.access_token:
            missing.append("access_token")
        if not self.app_key:
            missing.append("app_key")
        if not self.resource_id:
            missing.append("resource_id")
        if not self.websocket_url:
            missing.append("websocket_url")
        if missing:
            raise ValueError(f"missing Doubao S2S credential fields: {missing}")


@dataclass(frozen=True)
class DoubaoS2SSessionConfig:
    speaker: str = DEFAULT_SPEAKER
    input_sample_rate: int = DEFAULT_SAMPLE_RATE
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    bits: int = DEFAULT_BITS
    language: str = DEFAULT_LANGUAGE
    uid: str = "sip-realtime-voice-gateway"
    system_prompt: str = (
        "你是中文电话客服助手，回答要简短、自然、口语化。"
        "每次回答不超过两句。"
    )
    temperature: float = 0.3
    top_p: float = 0.9
    max_tokens: int = 256
    dialog: RealtimeDialogConfig = field(default_factory=RealtimeDialogConfig)

    def validate(self) -> None:
        if not self.speaker:
            raise ValueError("speaker is required")
        if self.input_sample_rate != DEFAULT_SAMPLE_RATE:
            raise ValueError("Doubao S2S probe expects 16 kHz input PCM")
        if self.output_sample_rate <= 0:
            raise ValueError("output_sample_rate must be positive")
        if self.channels != DEFAULT_CHANNELS:
            raise ValueError("only mono audio is supported")
        if self.bits != DEFAULT_BITS:
            raise ValueError("only 16-bit PCM is supported")
        if self.dialog.bot_name and len(self.dialog.bot_name) > 20:
            raise ValueError("dialog.bot_name must be at most 20 characters")


@dataclass(frozen=True)
class DoubaoS2SFrame:
    message_type: int
    flags: int
    serialization: int
    compression: int
    payload: bytes
    event: int | None = None
    session_id: str = ""
    connect_id: str = ""
    sequence: int | None = None
    error_code: int | None = None

    @property
    def payload_json(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        return json.loads(self.payload.decode("utf-8"))


@dataclass(frozen=True)
class DoubaoS2SEvent:
    event: int
    session_id: str
    connect_id: str
    payload: dict[str, Any]
    raw_payload: bytes
    audio: bytes
    text: str
    is_final: bool
    error: str | None = None
    req_id: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class DoubaoS2SProbeResult:
    session_id: str
    speaker: str
    input_text: str
    input_audio_bytes: int
    output_audio_bytes: int
    input_transcript: str
    output_transcript: str
    event_counts: dict[str, int]
    sanitized_events: list[dict[str, Any]]
    first_audio_delta_ms: int | None
    response_done_ms: int | None
    output_sample_rate: int = DEFAULT_SAMPLE_RATE


class DoubaoS2SRealtimeSession:
    def __init__(
        self,
        credentials: DoubaoS2SCredentials,
        config: DoubaoS2SSessionConfig,
    ) -> None:
        credentials.validate()
        config.validate()
        self.credentials = credentials
        self.config = config
        self.session_id = f"session_{uuid.uuid4().hex}"
        self.connect_id = credentials.connect_id or f"conn_{uuid.uuid4().hex}"
        self._ws = None
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            self._ws = await connect(
                self.credentials.websocket_url,
                extra_headers=build_websocket_headers(
                    self.credentials,
                    connect_id=self.connect_id,
                ),
                ping_interval=None,
                max_size=16 * 1024 * 1024,
            )
        except InvalidStatusCode as err:
            raise DoubaoS2SError(_format_handshake_error(err)) from err
        await self._send_frame(
            build_json_event_frame(EVENT_START_CONNECTION, {}, session_id="")
        )
        event = await self.recv_event()
        if event.error:
            raise DoubaoS2SError(event.error)
        if event.event != EVENT_CONNECTION_STARTED:
            raise DoubaoS2SError(f"unexpected connection event: {event.event}")
        if event.connect_id:
            self.connect_id = event.connect_id

    async def start_session(self) -> None:
        await self.send_start_session()
        event = await self.recv_event()
        if event.error:
            raise DoubaoS2SError(event.error)
        if event.event != EVENT_SESSION_STARTED:
            raise DoubaoS2SError(f"unexpected session event: {event.event}")
        if event.session_id:
            self.session_id = event.session_id

    async def send_start_session(self) -> None:
        await self._send_frame(
            build_json_event_frame(
                EVENT_START_SESSION,
                build_start_session_payload(self.config),
                session_id=self.session_id,
            )
        )

    async def close(self) -> None:
        if self._ws is None:
            return
        with contextlib.suppress(Exception):
            await self.finish_session()
        await self._ws.close()
        self._ws = None

    async def send_audio(self, pcm16_16k: bytes) -> None:
        if not pcm16_16k:
            return
        await self._send_frame(
            build_audio_event_frame(
                EVENT_TASK_AUDIO,
                pcm16_16k,
                session_id=self.session_id,
            )
        )

    async def send_user_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise ValueError("text is required")
        await self._send_json_event(EVENT_USER_TEXT, {"content": text})

    async def say_hello(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise ValueError("hello text is required")
        await self._send_json_event(EVENT_SAY_HELLO, {"content": text})

    async def send_tts_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise ValueError("tts text is required")
        await self._send_json_event(EVENT_TTS_TEXT, {"content": text})

    async def finish_session(self) -> None:
        await self._send_json_event(EVENT_FINISH_SESSION, {})

    async def client_interrupt(self) -> None:
        await self._send_json_event(EVENT_CLIENT_INTERRUPT, {})

    async def recv_event(self) -> DoubaoS2SEvent:
        if self._ws is None:
            raise RuntimeError("Doubao S2S session is not connected")
        raw_message = await self._ws.recv()
        if isinstance(raw_message, str):
            raise DoubaoS2SError(raw_message)
        frame = parse_frame(raw_message)
        return decode_event(frame)

    async def _send_json_event(self, event: int, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body.setdefault("session_id", self.session_id)
        await self._send_frame(
            build_json_event_frame(
                event,
                body,
                session_id=self.session_id,
            )
        )

    async def _send_frame(self, frame: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("Doubao S2S session is not connected")
        async with self._send_lock:
            await self._ws.send(frame)


async def run_doubao_s2s_text_probe(
    credentials: DoubaoS2SCredentials,
    config: DoubaoS2SSessionConfig,
    *,
    input_text: str,
    timeout_seconds: int = 60,
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None = None,
) -> tuple[DoubaoS2SProbeResult, bytes]:
    session = DoubaoS2SRealtimeSession(credentials, config)
    await session.connect()
    await session.start_session()
    started_at = time.monotonic()
    await session.send_user_text(input_text)
    try:
        return await _collect_probe_result(
            session,
            config,
            input_text=input_text,
            input_audio_bytes=0,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            on_audio_delta=on_audio_delta,
        )
    finally:
        with contextlib.suppress(Exception):
            await session.close()


async def run_doubao_s2s_audio_probe(
    credentials: DoubaoS2SCredentials,
    config: DoubaoS2SSessionConfig,
    *,
    input_pcm16_16k: bytes,
    timeout_seconds: int = 60,
    chunk_ms: int = 20,
    send_delay_ms: int = 20,
    trailing_silence_ms: int = 1200,
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None = None,
) -> tuple[DoubaoS2SProbeResult, bytes]:
    if not input_pcm16_16k:
        raise ValueError("input_pcm16_16k is required")

    session = DoubaoS2SRealtimeSession(credentials, config)
    await session.connect()
    await session.start_session()
    started_at = time.monotonic()
    try:
        audio_to_send = input_pcm16_16k + silence_pcm16(
            config.input_sample_rate,
            trailing_silence_ms,
        )
        frame_bytes = pcm_s16le_frame_bytes(config.input_sample_rate, chunk_ms)
        for chunk in split_audio_frames(audio_to_send, frame_bytes, pad_last=True):
            await session.send_audio(chunk)
            if send_delay_ms > 0:
                await asyncio.sleep(send_delay_ms / 1000)

        return await _collect_probe_result(
            session,
            config,
            input_text="",
            input_audio_bytes=len(input_pcm16_16k),
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            on_audio_delta=on_audio_delta,
        )
    finally:
        with contextlib.suppress(Exception):
            await session.close()


async def _collect_probe_result(
    session: DoubaoS2SRealtimeSession,
    config: DoubaoS2SSessionConfig,
    *,
    input_text: str,
    input_audio_bytes: int,
    started_at: float,
    timeout_seconds: int,
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None,
) -> tuple[DoubaoS2SProbeResult, bytes]:
    output_audio = bytearray()
    input_transcript = ""
    output_transcript_parts: list[str] = []
    event_counts: Counter[str] = Counter()
    sanitized_events: list[dict[str, Any]] = []
    first_audio_delta_ms: int | None = None
    response_done_ms: int | None = None

    async with asyncio.timeout(timeout_seconds):
        while True:
            event = await session.recv_event()
            event_counts[str(event.event)] += 1
            sanitized_events.append(sanitize_event(event))

            if event.error or event.event in ERROR_EVENTS:
                raise DoubaoS2SError(event.error or json.dumps(event.payload))

            if event.event in {EVENT_ASR_INFO, EVENT_ASR_RESPONSE, EVENT_ASR_ENDED}:
                if event.text:
                    input_transcript = event.text
                continue

            if event.event in {EVENT_CHAT_RESPONSE, EVENT_CHAT_ENDED}:
                if event.text:
                    output_transcript_parts.append(event.text)

            if event.audio:
                if first_audio_delta_ms is None:
                    first_audio_delta_ms = elapsed_ms(started_at)
                output_audio.extend(event.audio)
                if on_audio_delta is not None:
                    await on_audio_delta(event.audio)

            if event.event in TERMINAL_EVENTS:
                response_done_ms = elapsed_ms(started_at)
                break

    result = DoubaoS2SProbeResult(
        session_id=session.session_id,
        speaker=config.speaker,
        input_text=input_text,
        input_audio_bytes=input_audio_bytes,
        output_audio_bytes=len(output_audio),
        input_transcript=input_transcript,
        output_transcript="".join(output_transcript_parts),
        event_counts=dict(event_counts),
        sanitized_events=sanitized_events,
        first_audio_delta_ms=first_audio_delta_ms,
        response_done_ms=response_done_ms,
        output_sample_rate=config.output_sample_rate,
    )
    return result, bytes(output_audio)


def build_websocket_headers(
    credentials: DoubaoS2SCredentials,
    *,
    connect_id: str | None = None,
) -> dict[str, str]:
    credentials.validate()
    resolved_connect_id = connect_id or credentials.connect_id or uuid.uuid4().hex
    return {
        "X-Api-App-ID": credentials.app_id,
        "X-Api-App-Key": credentials.app_key,
        "X-Api-Access-Key": credentials.access_token,
        "X-Api-Resource-Id": credentials.resource_id,
        "X-Api-Connect-Id": resolved_connect_id,
        "X-Api-Request-Id": resolved_connect_id,
        "X-Tt-Logid": make_log_id(),
    }


def build_start_session_payload(config: DoubaoS2SSessionConfig) -> dict[str, Any]:
    config.validate()
    payload: dict[str, Any] = {
        "asr": {
            "language": config.language,
        },
        "tts": {
            "speaker": config.speaker,
            "audio_config": {
                "channel": config.channels,
                "format": "pcm",
                "sample_rate": config.output_sample_rate,
                "bits": config.bits,
            },
        },
        "dialog": _build_dialog_payload(config.dialog),
        "prompt": {
            "system": config.system_prompt,
        },
        "props": {
            "temperature": config.temperature,
            "top_p": config.top_p,
            "max_tokens": config.max_tokens,
        },
    }
    return payload


def _build_dialog_payload(config: RealtimeDialogConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config.bot_name:
        payload["bot_name"] = config.bot_name
    if config.system_role:
        payload["system_role"] = config.system_role
    if config.speaking_style:
        payload["speaking_style"] = config.speaking_style
    if config.dialog_id:
        payload["dialog_id"] = config.dialog_id
    if config.dialog_context:
        payload["dialog_context"] = [
            item.to_payload() for item in config.dialog_context
        ]
    if config.model:
        payload["extra"] = {"model": config.model}
    return payload


def build_json_event_frame(
    event: int,
    payload: dict[str, Any],
    *,
    session_id: str,
) -> bytes:
    return build_event_frame(
        message_type=MESSAGE_TYPE_FULL_CLIENT,
        event=event,
        session_id=session_id,
        serialization=SERIALIZATION_JSON,
        compression=COMPRESSION_NONE,
        payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def build_audio_event_frame(
    event: int,
    payload: bytes,
    *,
    session_id: str,
) -> bytes:
    return build_event_frame(
        message_type=MESSAGE_TYPE_AUDIO_ONLY_CLIENT,
        event=event,
        session_id=session_id,
        serialization=SERIALIZATION_NONE,
        compression=COMPRESSION_NONE,
        payload=payload,
    )


def build_event_frame(
    *,
    message_type: int,
    event: int,
    payload: bytes,
    session_id: str = "",
    connect_id: str = "",
    serialization: int = SERIALIZATION_JSON,
    compression: int = COMPRESSION_NONE,
) -> bytes:
    body = bytearray()
    body.extend(
        [
            (VERSION_1 << 4) | HEADER_SIZE_UNITS,
            (message_type << 4) | FLAG_WITH_EVENT,
            (serialization << 4) | compression,
            0,
        ]
    )
    body.extend(event.to_bytes(4, "big", signed=False))
    if has_session_id_field(event):
        _append_sized_string(body, session_id)
    if has_connect_id_field(event):
        _append_sized_string(body, connect_id)
    if message_type == MESSAGE_TYPE_ERROR:
        body.extend((0).to_bytes(4, "big", signed=False))

    outbound_payload = gzip.compress(payload) if compression == COMPRESSION_GZIP else payload
    body.extend(len(outbound_payload).to_bytes(4, "big", signed=False))
    body.extend(outbound_payload)
    return bytes(body)


def parse_frame(data: bytes) -> DoubaoS2SFrame:
    if len(data) < 8:
        raise DoubaoS2SError(f"frame too short: {len(data)}")

    header_size = (data[0] & 0x0F) * 4
    if header_size <= 0 or len(data) < header_size:
        raise DoubaoS2SError(f"invalid header size: {header_size}")

    message_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F
    compression = data[2] & 0x0F
    offset = header_size
    sequence: int | None = None
    event: int | None = None
    session_id = ""
    connect_id = ""
    error_code: int | None = None

    if has_sequence_field(message_type, flags):
        _require_size(data, offset, 4)
        sequence = int.from_bytes(data[offset : offset + 4], "big", signed=True)
        offset += 4

    if flags & FLAG_WITH_EVENT:
        _require_size(data, offset, 4)
        event = int.from_bytes(data[offset : offset + 4], "big", signed=False)
        offset += 4
        if has_session_id_field(event):
            session_id, offset = _read_sized_string(data, offset)
        if has_connect_id_field(event):
            connect_id, offset = _read_sized_string(data, offset)

    if message_type == MESSAGE_TYPE_ERROR:
        _require_size(data, offset, 4)
        error_code = int.from_bytes(data[offset : offset + 4], "big", signed=False)
        offset += 4

    _require_size(data, offset, 4)
    payload_size = int.from_bytes(data[offset : offset + 4], "big", signed=False)
    offset += 4
    _require_size(data, offset, payload_size)
    payload = data[offset : offset + payload_size]
    if compression == COMPRESSION_GZIP and payload:
        payload = gzip.decompress(payload)

    return DoubaoS2SFrame(
        message_type=message_type,
        flags=flags,
        serialization=serialization,
        compression=compression,
        payload=payload,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        sequence=sequence,
        error_code=error_code,
    )


def decode_event(frame: DoubaoS2SFrame) -> DoubaoS2SEvent:
    payload = _decode_payload_json(frame.payload)
    audio = b""
    text = ""
    error = None
    is_final = False

    if frame.message_type == MESSAGE_TYPE_AUDIO_ONLY_SERVER:
        audio = frame.payload
    elif isinstance(payload.get("audio"), str):
        with contextlib.suppress(Exception):
            audio = base64.b64decode(payload["audio"])

    if frame.event == EVENT_TTS_AUDIO_DATA and audio:
        audio = float32le_to_pcm_s16le(audio)

    for key in ("content", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            text = value
            break

    asr_info = payload.get("asr_info")
    if isinstance(asr_info, dict):
        asr_text = asr_info.get("text")
        if isinstance(asr_text, str) and asr_text:
            text = asr_text
        is_final = is_final or bool(asr_info.get("is_final"))

    results = payload.get("results")
    if isinstance(results, list) and results:
        last_result = results[-1]
        if isinstance(last_result, dict):
            result_text = last_result.get("text")
            if isinstance(result_text, str) and result_text:
                text = result_text
            is_final = is_final or last_result.get("is_interim") is False

    tts_info = payload.get("tts_info")
    if isinstance(tts_info, dict):
        for key in ("content", "text"):
            tts_text = tts_info.get(key)
            if isinstance(tts_text, str) and tts_text:
                text = tts_text
                break

    is_final = is_final or bool(payload.get("is_final"))
    if frame.event in {
        EVENT_ASR_ENDED,
        EVENT_CHAT_ENDED,
        EVENT_TTS_FINISHED,
        EVENT_SESSION_FINISHED,
    }:
        is_final = True

    if frame.message_type == MESSAGE_TYPE_ERROR:
        error = _payload_error_message(payload) or f"error_code={frame.error_code}"
    elif frame.event in ERROR_EVENTS:
        error = _payload_error_message(payload) or f"event={frame.event}"

    return DoubaoS2SEvent(
        event=frame.event or 0,
        session_id=frame.session_id,
        connect_id=frame.connect_id,
        payload=payload,
        raw_payload=frame.payload,
        audio=audio,
        text=text,
        is_final=is_final,
        error=error,
        req_id=_optional_str(payload.get("reqid")),
        trace_id=_optional_str(payload.get("trace_id")),
    )


def sanitize_event(event: DoubaoS2SEvent) -> dict[str, Any]:
    payload = dict(event.payload)
    if "audio" in payload and isinstance(payload["audio"], str):
        payload["audio"] = f"<base64:{len(payload['audio'])} chars>"
    return {
        "event": event.event,
        "session_id": event.session_id,
        "connect_id": event.connect_id,
        "payload": payload,
        "audio_bytes": len(event.audio),
        "text": event.text,
        "is_final": event.is_final,
        "error": event.error,
        "req_id": event.req_id,
        "trace_id": event.trace_id,
    }


def has_sequence_field(message_type: int, flags: int) -> bool:
    if message_type == MESSAGE_TYPE_AUDIO_ONLY_CLIENT:
        return False
    return flags in {
        FLAG_POSITIVE_SEQUENCE,
        FLAG_NEGATIVE_SEQUENCE,
        FLAG_NEGATIVE_WITH_SEQUENCE,
    }


def has_session_id_field(event: int | None) -> bool:
    return event not in {
        EVENT_START_CONNECTION,
        EVENT_FINISH_CONNECTION,
        EVENT_CONNECTION_STARTED,
        EVENT_CONNECTION_FAILED,
        EVENT_CONNECTION_ENDED,
    }


def has_connect_id_field(event: int | None) -> bool:
    return event in {
        EVENT_CONNECTION_STARTED,
        EVENT_CONNECTION_FAILED,
        EVENT_CONNECTION_ENDED,
    }


def silence_pcm16(sample_rate: int, duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    return b"\x00" * pcm_s16le_frame_bytes(sample_rate, duration_ms)


def make_log_id() -> str:
    return f"02{int(time.time() * 1000)}{uuid.uuid4().hex[:16]}"


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _format_handshake_error(err: InvalidStatusCode) -> str:
    log_id = err.headers.get("X-Tt-Logid", "")
    status_code = err.headers.get("X-Api-Status-Code", "")
    message = err.headers.get("X-Api-Message", "")
    parts = [f"Doubao S2S websocket handshake failed: HTTP {err.status_code}"]
    if status_code:
        parts.append(f"api_status={status_code}")
    if message:
        parts.append(f"api_message={message}")
    if log_id:
        parts.append(f"x_tt_logid={log_id}")
    return ", ".join(parts)


def _decode_payload_json(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    with contextlib.suppress(Exception):
        value = json.loads(payload.decode("utf-8"))
        if isinstance(value, dict):
            return value
    return {}


def _payload_error_message(payload: dict[str, Any]) -> str | None:
    for key in ("message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _redact_provider_error(value)
    code = payload.get("code")
    if code:
        return f"code={code}"
    return None


def _redact_provider_error(message: str) -> str:
    if "invalid X-Api-App-Key:" in message:
        prefix = "invalid X-Api-App-Key:"
        before, _, after = message.partition(prefix)
        _, _, suffix = after.partition(", expected:")
        if suffix:
            return f"{before}{prefix} <redacted>, expected:{suffix}"
        return f"{before}{prefix} <redacted>"
    return message


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _append_sized_string(body: bytearray, value: str) -> None:
    encoded = value.encode("utf-8")
    body.extend(len(encoded).to_bytes(4, "big", signed=False))
    body.extend(encoded)


def _read_sized_string(data: bytes, offset: int) -> tuple[str, int]:
    _require_size(data, offset, 4)
    size = int.from_bytes(data[offset : offset + 4], "big", signed=False)
    offset += 4
    _require_size(data, offset, size)
    value = data[offset : offset + size].decode("utf-8")
    return value, offset + size


def _require_size(data: bytes, offset: int, size: int) -> None:
    if len(data) < offset + size:
        raise DoubaoS2SError(
            f"incomplete frame: need {offset + size}, got {len(data)}"
        )
