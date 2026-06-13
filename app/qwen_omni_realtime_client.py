from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websockets.legacy.client import connect
from websockets.legacy.exceptions import InvalidStatusCode

from .audio_codec import pcm_s16le_frame_bytes, split_audio_frames

DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen3-omni-flash-realtime"
DEFAULT_VOICE = "Cherry"
DEFAULT_INPUT_SAMPLE_RATE = 16000
DEFAULT_OUTPUT_SAMPLE_RATE = 24000
DEFAULT_INPUT_TRANSCRIPTION_MODEL = "qwen3-asr-flash-realtime"


class QwenOmniRealtimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class QwenOmniRealtimeCredentials:
    api_key: str
    websocket_url: str = DEFAULT_WS_URL
    model: str = DEFAULT_MODEL

    def validate(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("api_key")
        if not self.websocket_url:
            missing.append("websocket_url")
        if not self.model:
            missing.append("model")
        if missing:
            raise ValueError(f"missing Qwen Omni Realtime credential fields: {missing}")


@dataclass(frozen=True)
class QwenOmniRealtimeSessionConfig:
    voice: str = DEFAULT_VOICE
    instructions: str = (
        "你是中文电话客服助手，回答要简短、自然、口语化。"
        "每次回答不超过两句。"
    )
    input_sample_rate: int = DEFAULT_INPUT_SAMPLE_RATE
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    input_audio_format: str = "pcm"
    output_audio_format: str = "pcm"
    language: str = "zh"
    modalities: tuple[str, ...] = ("text", "audio")
    manual_turn_detection: bool = True
    turn_detection_type: str = "server_vad"
    turn_detection_threshold: float = 0.5
    silence_duration_ms: int = 800
    enable_input_transcription: bool = True
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None

    def validate(self) -> None:
        if not self.voice:
            raise ValueError("voice is required")
        if self.input_sample_rate != DEFAULT_INPUT_SAMPLE_RATE:
            raise ValueError("Qwen Omni Realtime expects 16 kHz input PCM")
        if self.output_sample_rate != DEFAULT_OUTPUT_SAMPLE_RATE:
            raise ValueError("Qwen Omni Realtime outputs 24 kHz PCM")
        if self.input_audio_format != "pcm":
            raise ValueError("input_audio_format must be pcm")
        if self.output_audio_format != "pcm":
            raise ValueError("output_audio_format must be pcm")
        if set(self.modalities) not in ({"text"}, {"text", "audio"}):
            raise ValueError("modalities must be ['text'] or ['text', 'audio']")
        if self.silence_duration_ms < 200 or self.silence_duration_ms > 6000:
            raise ValueError("silence_duration_ms must be between 200 and 6000")


@dataclass(frozen=True)
class QwenOmniRealtimeUsage:
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_text_tokens: int = 0
    input_audio_tokens: int = 0
    output_text_tokens: int = 0
    output_audio_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QwenOmniRealtimeCostLineItem:
    tokens: int
    unit_price_rmb_per_million: float
    rmb: float
    billed: bool = True


@dataclass(frozen=True)
class QwenOmniRealtimeCostEstimate:
    total_rmb: float
    line_items: dict[str, QwenOmniRealtimeCostLineItem]


@dataclass(frozen=True)
class QwenOmniRealtimePrice:
    input_text_rmb_per_million: float
    input_audio_rmb_per_million: float
    output_text_rmb_per_million: float
    output_audio_rmb_per_million: float


@dataclass(frozen=True)
class QwenOmniRealtimeEvent:
    type: str
    payload: dict[str, Any]
    audio: bytes = b""
    text: str = ""
    error: str | None = None


@dataclass(frozen=True)
class QwenOmniRealtimeProbeResult:
    session_id: str
    model: str
    voice: str
    input_audio_bytes: int
    output_audio_bytes: int
    input_transcript: str
    output_transcript: str
    usage: QwenOmniRealtimeUsage
    cost_estimate_rmb: float | None
    cost_line_items: dict[str, dict[str, Any]]
    event_counts: dict[str, int]
    sanitized_events: list[dict[str, Any]]
    first_audio_delta_ms: int | None
    response_done_ms: int | None
    input_sample_rate: int = DEFAULT_INPUT_SAMPLE_RATE
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE


MAINLAND_PRICES_RMB_PER_MILLION: dict[str, QwenOmniRealtimePrice] = {
    "qwen3.5-omni-plus-realtime": QwenOmniRealtimePrice(10, 80, 60, 300),
    "qwen3.5-omni-flash-realtime": QwenOmniRealtimePrice(3.3, 27, 20, 107),
    "qwen3-omni-flash-realtime": QwenOmniRealtimePrice(2.2, 18.9, 15.2, 75.1),
    "qwen-omni-turbo-realtime": QwenOmniRealtimePrice(1.6, 25, 18, 50),
}


class QwenOmniRealtimeSession:
    def __init__(
        self,
        credentials: QwenOmniRealtimeCredentials,
        config: QwenOmniRealtimeSessionConfig,
    ) -> None:
        credentials.validate()
        config.validate()
        self.credentials = credentials
        self.config = config
        self._ws = None
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            self._ws = await connect(
                build_websocket_url(self.credentials),
                extra_headers=build_websocket_headers(self.credentials),
                ping_interval=None,
                max_size=16 * 1024 * 1024,
            )
        except InvalidStatusCode as err:
            raise QwenOmniRealtimeError(_format_handshake_error(err)) from err

    async def close(self) -> None:
        if self._ws is None:
            return
        await self._ws.close()
        self._ws = None

    async def send_session_update(self) -> None:
        await self._send_json(build_session_update_event(self.config))

    async def send_audio(self, pcm16_16k: bytes) -> None:
        if not pcm16_16k:
            return
        await self._send_json(build_input_audio_append_event(pcm16_16k))

    async def commit_audio(self) -> None:
        await self._send_json(
            {"event_id": _event_id(), "type": "input_audio_buffer.commit"}
        )

    async def create_response(self) -> None:
        await self._send_json({"event_id": _event_id(), "type": "response.create"})

    async def recv_event(self) -> QwenOmniRealtimeEvent:
        if self._ws is None:
            raise RuntimeError("Qwen Omni Realtime session is not connected")
        raw_message = await self._ws.recv()
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        payload = json.loads(raw_message)
        if not isinstance(payload, dict):
            raise QwenOmniRealtimeError("invalid Qwen event payload")
        return decode_event(payload)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("Qwen Omni Realtime session is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))


async def run_qwen_omni_realtime_audio_probe(
    credentials: QwenOmniRealtimeCredentials,
    config: QwenOmniRealtimeSessionConfig,
    *,
    input_pcm16_16k: bytes,
    timeout_seconds: int = 60,
    chunk_ms: int = 20,
    send_delay_ms: int = 20,
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None = None,
) -> tuple[QwenOmniRealtimeProbeResult, bytes]:
    if not input_pcm16_16k:
        raise ValueError("input_pcm16_16k is required")

    session = QwenOmniRealtimeSession(credentials, config)
    await session.connect()
    started_at = time.monotonic()
    try:
        await session.send_session_update()
        frame_bytes = pcm_s16le_frame_bytes(config.input_sample_rate, chunk_ms)
        for chunk in split_audio_frames(input_pcm16_16k, frame_bytes, pad_last=True):
            await session.send_audio(chunk)
            if send_delay_ms > 0:
                await asyncio.sleep(send_delay_ms / 1000)
        if config.manual_turn_detection:
            await session.commit_audio()
            await session.create_response()

        return await _collect_probe_result(
            session,
            credentials,
            config,
            input_audio_bytes=len(input_pcm16_16k),
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            on_audio_delta=on_audio_delta,
        )
    finally:
        with contextlib.suppress(Exception):
            await session.close()


async def _collect_probe_result(
    session: QwenOmniRealtimeSession,
    credentials: QwenOmniRealtimeCredentials,
    config: QwenOmniRealtimeSessionConfig,
    *,
    input_audio_bytes: int,
    started_at: float,
    timeout_seconds: int,
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None,
) -> tuple[QwenOmniRealtimeProbeResult, bytes]:
    output_audio = bytearray()
    input_transcript = ""
    output_transcript = ""
    output_transcript_parts: list[str] = []
    event_counts: Counter[str] = Counter()
    sanitized_events: list[dict[str, Any]] = []
    first_audio_delta_ms: int | None = None
    response_done_ms: int | None = None
    session_id = ""
    usage = QwenOmniRealtimeUsage()

    async with asyncio.timeout(timeout_seconds):
        while True:
            event = await session.recv_event()
            event_counts[event.type] += 1
            sanitized_events.append(sanitize_event(event))
            if event.error:
                raise QwenOmniRealtimeError(event.error)

            if event.type == "session.created":
                session_payload = event.payload.get("session")
                if isinstance(session_payload, dict):
                    value = session_payload.get("id")
                    if isinstance(value, str):
                        session_id = value
                continue

            if event.type == "conversation.item.input_audio_transcription.completed":
                if event.text:
                    input_transcript = event.text
                continue

            if event.type == "response.audio_transcript.delta":
                if event.text:
                    output_transcript_parts.append(event.text)
                continue

            if event.type in {"response.audio_transcript.done", "response.text.done"}:
                if event.text:
                    output_transcript = event.text
                continue

            if event.audio:
                if first_audio_delta_ms is None:
                    first_audio_delta_ms = elapsed_ms(started_at)
                output_audio.extend(event.audio)
                if on_audio_delta is not None:
                    await on_audio_delta(event.audio)
                continue

            if event.type == "response.done":
                usage = parse_response_usage(event.payload)
                response_done_ms = elapsed_ms(started_at)
                break

    if not output_transcript and output_transcript_parts:
        output_transcript = "".join(output_transcript_parts)

    cost_estimate = estimate_usage_cost_rmb(
        usage,
        model=credentials.model,
        output_audio_enabled="audio" in config.modalities,
    )
    result = QwenOmniRealtimeProbeResult(
        session_id=session_id,
        model=credentials.model,
        voice=config.voice,
        input_audio_bytes=input_audio_bytes,
        output_audio_bytes=len(output_audio),
        input_transcript=input_transcript,
        output_transcript=output_transcript,
        usage=usage,
        cost_estimate_rmb=cost_estimate.total_rmb if cost_estimate else None,
        cost_line_items=_cost_line_items_to_dict(cost_estimate),
        event_counts=dict(event_counts),
        sanitized_events=sanitized_events,
        first_audio_delta_ms=first_audio_delta_ms,
        response_done_ms=response_done_ms,
        input_sample_rate=config.input_sample_rate,
        output_sample_rate=config.output_sample_rate,
    )
    return result, bytes(output_audio)


def build_websocket_url(credentials: QwenOmniRealtimeCredentials) -> str:
    credentials.validate()
    parsed = urlsplit(credentials.websocket_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["model"] = credentials.model
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def build_websocket_headers(
    credentials: QwenOmniRealtimeCredentials,
) -> dict[str, str]:
    credentials.validate()
    return {"Authorization": f"Bearer {credentials.api_key}"}


def build_session_update_event(
    config: QwenOmniRealtimeSessionConfig,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    config.validate()
    session: dict[str, Any] = {
        "modalities": list(config.modalities),
        "voice": config.voice,
        "input_audio_format": config.input_audio_format,
        "output_audio_format": config.output_audio_format,
        "instructions": config.instructions,
        "turn_detection": None
        if config.manual_turn_detection
        else {
            "type": config.turn_detection_type,
            "threshold": config.turn_detection_threshold,
            "silence_duration_ms": config.silence_duration_ms,
        },
    }
    if config.enable_input_transcription:
        session["input_audio_transcription"] = {
            "model": DEFAULT_INPUT_TRANSCRIPTION_MODEL,
            "language": config.language,
        }
    for key, value in {
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
    }.items():
        if value is not None:
            session[key] = value

    return {
        "event_id": event_id or _event_id(),
        "type": "session.update",
        "session": session,
    }


def build_input_audio_append_event(
    pcm16_16k: bytes,
    *,
    event_id: str | None = None,
) -> dict[str, str]:
    return {
        "event_id": event_id or _event_id(),
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm16_16k).decode("ascii"),
    }


def decode_event(payload: dict[str, Any]) -> QwenOmniRealtimeEvent:
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        raise QwenOmniRealtimeError("missing Qwen event type")

    audio = b""
    delta = payload.get("delta")
    if event_type == "response.audio.delta" and isinstance(delta, str) and delta:
        try:
            audio = base64.b64decode(delta)
        except ValueError as err:
            raise QwenOmniRealtimeError("invalid Qwen audio delta") from err

    text = ""
    if event_type in {
        "conversation.item.input_audio_transcription.completed",
        "response.audio_transcript.done",
    }:
        text_value = payload.get("transcript")
        if isinstance(text_value, str):
            text = text_value
    elif event_type == "response.audio_transcript.delta":
        text_value = payload.get("delta")
        if isinstance(text_value, str):
            text = text_value
    elif event_type == "response.text.done":
        text_value = payload.get("text")
        if isinstance(text_value, str):
            text = text_value

    error = None
    if event_type == "error":
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message") or error_payload.get("code")
            error = str(message) if message else json.dumps(error_payload)
        else:
            error = json.dumps(payload, ensure_ascii=False)

    return QwenOmniRealtimeEvent(
        type=event_type,
        payload=payload,
        audio=audio,
        text=text,
        error=error,
    )


def sanitize_event(event: QwenOmniRealtimeEvent) -> dict[str, Any]:
    payload = dict(event.payload)
    for key in ("audio", "delta"):
        if key in payload and isinstance(payload[key], str):
            if event.type == "response.audio.delta" or key == "audio":
                payload[key] = f"<base64:{len(payload[key])} chars>"
    return {
        "type": event.type,
        "payload": payload,
        "audio_bytes": len(event.audio),
        "text": event.text,
        "error": event.error,
    }


def parse_response_usage(payload: dict[str, Any]) -> QwenOmniRealtimeUsage:
    response = payload.get("response")
    usage = response.get("usage") if isinstance(response, dict) else payload.get("usage")
    if not isinstance(usage, dict):
        return QwenOmniRealtimeUsage()

    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    if not isinstance(output_details, dict):
        output_details = {}

    return QwenOmniRealtimeUsage(
        total_tokens=_int_value(usage.get("total_tokens")),
        input_tokens=_int_value(usage.get("input_tokens")),
        output_tokens=_int_value(usage.get("output_tokens")),
        input_text_tokens=_int_value(input_details.get("text_tokens")),
        input_audio_tokens=_int_value(input_details.get("audio_tokens")),
        output_text_tokens=_int_value(output_details.get("text_tokens")),
        output_audio_tokens=_int_value(output_details.get("audio_tokens")),
        raw=dict(usage),
    )


def estimate_usage_cost_rmb(
    usage: QwenOmniRealtimeUsage,
    *,
    model: str,
    output_audio_enabled: bool,
) -> QwenOmniRealtimeCostEstimate | None:
    prices = price_for_model(model)
    if prices is None:
        return None

    line_items = {
        "input_text": _cost_line(
            usage.input_text_tokens,
            prices.input_text_rmb_per_million,
        ),
        "input_audio": _cost_line(
            usage.input_audio_tokens,
            prices.input_audio_rmb_per_million,
        ),
    }
    if output_audio_enabled:
        line_items["output_text"] = _cost_line(
            usage.output_text_tokens,
            prices.output_text_rmb_per_million,
            billed=False,
        )
        line_items["output_audio"] = _cost_line(
            usage.output_audio_tokens,
            prices.output_audio_rmb_per_million,
        )
    else:
        line_items["output_text"] = _cost_line(
            usage.output_text_tokens,
            prices.output_text_rmb_per_million,
        )
        line_items["output_audio"] = _cost_line(
            usage.output_audio_tokens,
            prices.output_audio_rmb_per_million,
            billed=False,
        )

    return QwenOmniRealtimeCostEstimate(
        total_rmb=sum(item.rmb for item in line_items.values()),
        line_items=line_items,
    )


def price_for_model(model: str) -> QwenOmniRealtimePrice | None:
    for prefix, prices in MAINLAND_PRICES_RMB_PER_MILLION.items():
        if model == prefix or model.startswith(f"{prefix}-"):
            return prices
    return None


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _cost_line(
    tokens: int,
    unit_price_rmb_per_million: float,
    *,
    billed: bool = True,
) -> QwenOmniRealtimeCostLineItem:
    rmb = tokens * unit_price_rmb_per_million / 1_000_000 if billed else 0.0
    return QwenOmniRealtimeCostLineItem(
        tokens=tokens,
        unit_price_rmb_per_million=unit_price_rmb_per_million,
        rmb=rmb,
        billed=billed,
    )


def _cost_line_items_to_dict(
    estimate: QwenOmniRealtimeCostEstimate | None,
) -> dict[str, dict[str, Any]]:
    if estimate is None:
        return {}
    return {
        key: {
            "tokens": item.tokens,
            "unit_price_rmb_per_million": item.unit_price_rmb_per_million,
            "rmb": item.rmb,
            "billed": item.billed,
        }
        for key, item in estimate.line_items.items()
    }


def _format_handshake_error(err: InvalidStatusCode) -> str:
    return f"Qwen Omni Realtime websocket handshake failed: HTTP {err.status_code}"


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
