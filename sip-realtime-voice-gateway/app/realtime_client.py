from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from websockets.legacy.client import connect

from .audio_codec import pcm_s16le_frame_bytes, split_audio_frames
from .config import RealtimeConfig

DEFAULT_INPUT_SAMPLE_RATE = 16000
DEFAULT_OUTPUT_SAMPLE_RATE = 24000
DEFAULT_CHUNK_MS = 100


class RealtimeAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class RealtimeProbeResult:
    model: str
    voice: str
    input_audio_bytes: int
    output_audio_bytes: int
    input_transcript: str
    output_transcript: str
    event_counts: dict[str, int]
    sanitized_events: list[dict[str, Any]]
    first_audio_delta_ms: int | None
    response_done_ms: int | None
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE


@dataclass(frozen=True)
class ServerVadProbeResult:
    model: str
    voice: str
    input_audio_bytes: int
    trailing_silence_ms: int
    output_audio_bytes: int
    input_transcript: str
    output_transcript: str
    event_counts: dict[str, int]
    sanitized_events: list[dict[str, Any]]
    response_ids: list[str]
    item_ids: list[str]
    speech_started_event_ms: int | None
    speech_stopped_event_ms: int | None
    response_created_ms: int | None
    first_audio_delta_ms: int | None
    response_done_ms: int | None
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE


@dataclass(frozen=True)
class RealtimeTurnResult:
    turn_id: int
    input_audio_bytes: int
    output_audio_bytes: int
    input_transcript: str
    output_transcript: str
    event_counts: dict[str, int]
    first_audio_delta_ms: int | None
    response_done_ms: int | None
    status: str = "completed"


@dataclass
class _RealtimeTurnState:
    turn_id: int
    input_audio_bytes: int
    started_at: float
    future: asyncio.Future[RealtimeTurnResult]
    event_counts: Counter[str]
    output_audio_bytes: int = 0
    first_audio_delta_ms: int | None = None
    input_transcripts: list[str] | None = None
    output_transcript_parts: list[str] | None = None

    def __post_init__(self) -> None:
        if self.input_transcripts is None:
            self.input_transcripts = []
        if self.output_transcript_parts is None:
            self.output_transcript_parts = []


class RealtimeStreamingSession:
    """Persistent Realtime API session for one phone call."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        api_key: str,
        instructions: str,
        on_audio_delta: Callable[[int, bytes], Awaitable[None]],
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.config = config
        self.api_key = api_key
        self.instructions = instructions
        self.on_audio_delta = on_audio_delta
        self._ws = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._active_turn_id: int | None = None
        self._turns: dict[int, _RealtimeTurnState] = {}
        self._closed = False

    @property
    def active_turn_id(self) -> int | None:
        return self._active_turn_id

    async def connect(self) -> None:
        url = build_realtime_url(self.config.url, self.config.model)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        self._ws = await connect(
            url,
            extra_headers=headers,
            ping_interval=None,
            max_size=8 * 1024 * 1024,
        )
        await self._send_event(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "voice": self.config.voice,
                    "instructions": self.instructions,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime",
                    },
                    "turn_detection": None,
                },
            },
        )
        self._reader_task = asyncio.create_task(
            self._read_events(),
            name="realtime-session-reader",
        )

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        for state in self._turns.values():
            if not state.future.done():
                state.future.cancel()
        self._turns.clear()
        self._active_turn_id = None

    async def append_audio(self, input_pcm_16k: bytes) -> None:
        if not input_pcm_16k:
            return
        await self._send_event(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(input_pcm_16k).decode("ascii"),
            },
        )

    async def clear_input_buffer(self) -> None:
        await self._send_event({"type": "input_audio_buffer.clear"})

    async def commit_and_create_response(
        self,
        *,
        turn_id: int,
        input_audio_bytes: int,
    ) -> asyncio.Future[RealtimeTurnResult]:
        if self._active_turn_id is not None:
            raise RuntimeError("a realtime response is already active")

        future: asyncio.Future[RealtimeTurnResult] = asyncio.get_running_loop().create_future()
        self._active_turn_id = turn_id
        self._turns[turn_id] = _RealtimeTurnState(
            turn_id=turn_id,
            input_audio_bytes=input_audio_bytes,
            started_at=time.monotonic(),
            future=future,
            event_counts=Counter(),
        )
        await self._send_event({"type": "input_audio_buffer.commit"})
        await self._send_event({"type": "response.create"})
        return future

    async def cancel_response(self) -> None:
        active_turn_id = self._active_turn_id
        if active_turn_id is None:
            return

        state = self._turns.pop(active_turn_id, None)
        self._active_turn_id = None
        if state is not None and not state.future.done():
            state.future.cancel()

        await self._send_event({"type": "response.cancel"})

    async def _send_event(self, event: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("realtime session is not connected")
        async with self._send_lock:
            await _send_event(self._ws, event)

    async def _read_events(self) -> None:
        assert self._ws is not None
        async for raw_message in self._ws:
            if not isinstance(raw_message, str):
                continue

            event = json.loads(raw_message)
            event_type = str(event.get("type", "unknown"))
            active_turn_id = self._active_turn_id
            state = self._turns.get(active_turn_id) if active_turn_id is not None else None

            if event_type == "error":
                if state is not None and not state.future.done():
                    state.future.set_exception(
                        RealtimeAPIError(json.dumps(event.get("error", event)))
                    )
                    self._turns.pop(state.turn_id, None)
                    self._active_turn_id = None
                continue

            if state is None:
                continue

            state.event_counts[event_type] += 1

            if event_type == "conversation.item.input_audio_transcription.completed":
                assert state.input_transcripts is not None
                state.input_transcripts.append(str(event.get("transcript", "")))
                continue

            if event_type == "response.audio_transcript.delta":
                assert state.output_transcript_parts is not None
                state.output_transcript_parts.append(str(event.get("delta", "")))
                continue

            if event_type == "response.audio_transcript.done":
                transcript = str(event.get("transcript", ""))
                if transcript:
                    state.output_transcript_parts = [transcript]
                continue

            if event_type == "response.audio.delta":
                audio_delta = base64.b64decode(event.get("delta", ""))
                if state.first_audio_delta_ms is None:
                    state.first_audio_delta_ms = int(
                        (time.monotonic() - state.started_at) * 1000
                    )
                state.output_audio_bytes += len(audio_delta)
                await self.on_audio_delta(state.turn_id, audio_delta)
                continue

            if event_type == "response.done":
                self._complete_turn(state, event)

    def _complete_turn(
        self,
        state: _RealtimeTurnState,
        event: dict[str, Any],
    ) -> None:
        if state.future.done():
            return

        response = event.get("response", {})
        status = "completed"
        if isinstance(response, dict):
            status = str(response.get("status", status))

        result = RealtimeTurnResult(
            turn_id=state.turn_id,
            input_audio_bytes=state.input_audio_bytes,
            output_audio_bytes=state.output_audio_bytes,
            input_transcript="".join(state.input_transcripts or []),
            output_transcript="".join(state.output_transcript_parts or []),
            event_counts=dict(state.event_counts),
            first_audio_delta_ms=state.first_audio_delta_ms,
            response_done_ms=int((time.monotonic() - state.started_at) * 1000),
            status=status,
        )
        state.future.set_result(result)
        self._turns.pop(state.turn_id, None)
        if self._active_turn_id == state.turn_id:
            self._active_turn_id = None


async def run_realtime_probe(
    config: RealtimeConfig,
    *,
    api_key: str,
    input_pcm: bytes,
    instructions: str,
    chunk_ms: int = DEFAULT_CHUNK_MS,
    timeout_seconds: int = 60,
    send_delay_ms: int = 5,
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None = None,
) -> tuple[RealtimeProbeResult, bytes]:
    if not api_key:
        raise ValueError("api_key is required")
    if not input_pcm:
        raise ValueError("input_pcm is required")

    url = build_realtime_url(config.url, config.model)
    headers = {"Authorization": f"Bearer {api_key}"}
    event_counter: Counter[str] = Counter()
    sanitized_events: list[dict[str, Any]] = []
    output_audio = bytearray()
    input_transcripts: list[str] = []
    output_transcript_parts: list[str] = []
    started_at = time.monotonic()
    first_audio_delta_ms: int | None = None
    response_done_ms: int | None = None

    async with connect(
        url,
        extra_headers=headers,
        ping_interval=None,
        max_size=8 * 1024 * 1024,
    ) as ws:
        await _send_event(
            ws,
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "voice": config.voice,
                    "instructions": instructions,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime",
                    },
                    "turn_detection": None,
                },
            },
        )

        frame_bytes = pcm_s16le_frame_bytes(DEFAULT_INPUT_SAMPLE_RATE, chunk_ms)
        for chunk in split_audio_frames(input_pcm, frame_bytes, pad_last=True):
            await _send_event(
                ws,
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                },
            )
            if send_delay_ms > 0:
                await asyncio.sleep(send_delay_ms / 1000)

        await _send_event(ws, {"type": "input_audio_buffer.commit"})
        await _send_event(ws, {"type": "response.create"})

        deadline = started_at + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for response.done")

            raw_message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if not isinstance(raw_message, str):
                continue

            event = json.loads(raw_message)
            event_type = str(event.get("type", "unknown"))
            event_counter[event_type] += 1
            sanitized_events.append(sanitize_event(event))

            if event_type == "error":
                raise RealtimeAPIError(json.dumps(event.get("error", event)))

            if event_type == "conversation.item.input_audio_transcription.completed":
                input_transcripts.append(str(event.get("transcript", "")))
                continue

            if event_type == "response.audio_transcript.delta":
                output_transcript_parts.append(str(event.get("delta", "")))
                continue

            if event_type == "response.audio_transcript.done":
                transcript = str(event.get("transcript", ""))
                if transcript:
                    output_transcript_parts = [transcript]
                continue

            if event_type == "response.audio.delta":
                if first_audio_delta_ms is None:
                    first_audio_delta_ms = int((time.monotonic() - started_at) * 1000)
                audio_delta = base64.b64decode(event.get("delta", ""))
                output_audio.extend(audio_delta)
                if on_audio_delta is not None:
                    await on_audio_delta(audio_delta)
                continue

            if event_type == "response.done":
                response_done_ms = int((time.monotonic() - started_at) * 1000)
                break

    return (
        RealtimeProbeResult(
            model=config.model,
            voice=config.voice,
            input_audio_bytes=len(input_pcm),
            output_audio_bytes=len(output_audio),
            input_transcript="".join(input_transcripts),
            output_transcript="".join(output_transcript_parts),
            event_counts=dict(event_counter),
            sanitized_events=sanitized_events,
            first_audio_delta_ms=first_audio_delta_ms,
            response_done_ms=response_done_ms,
        ),
        bytes(output_audio),
    )


async def run_server_vad_probe(
    config: RealtimeConfig,
    *,
    api_key: str,
    input_pcm: bytes,
    instructions: str,
    threshold: float = 0.5,
    silence_duration_ms: int = 800,
    prefix_padding_ms: int = 300,
    trailing_silence_ms: int = 1200,
    chunk_ms: int = DEFAULT_CHUNK_MS,
    timeout_seconds: int = 60,
    send_delay_ms: int = DEFAULT_CHUNK_MS,
) -> tuple[ServerVadProbeResult, bytes]:
    if not api_key:
        raise ValueError("api_key is required")
    if not input_pcm:
        raise ValueError("input_pcm is required")

    url = build_realtime_url(config.url, config.model)
    headers = {"Authorization": f"Bearer {api_key}"}
    event_counter: Counter[str] = Counter()
    sanitized_events: list[dict[str, Any]] = []
    output_audio = bytearray()
    input_transcripts: list[str] = []
    input_transcript_delta = ""
    output_transcript_parts: list[str] = []
    response_ids: list[str] = []
    item_ids: list[str] = []
    started_at = time.monotonic()
    speech_started_event_ms: int | None = None
    speech_stopped_event_ms: int | None = None
    response_created_ms: int | None = None
    first_audio_delta_ms: int | None = None
    response_done_ms: int | None = None

    async with connect(
        url,
        extra_headers=headers,
        ping_interval=None,
        max_size=8 * 1024 * 1024,
    ) as ws:
        await _send_event(
            ws,
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "voice": config.voice,
                    "instructions": instructions,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime",
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": threshold,
                        "prefix_padding_ms": prefix_padding_ms,
                        "silence_duration_ms": silence_duration_ms,
                    },
                },
            },
        )

        speech_stopped = asyncio.Event()

        async def read_events() -> None:
            nonlocal input_transcript_delta
            nonlocal output_transcript_parts
            nonlocal speech_started_event_ms, speech_stopped_event_ms
            nonlocal response_created_ms, first_audio_delta_ms, response_done_ms

            deadline = time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for server VAD response.done")

                raw_message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                if not isinstance(raw_message, str):
                    continue

                event = json.loads(raw_message)
                event_type = str(event.get("type", "unknown"))
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                event_counter[event_type] += 1
                sanitized_events.append(sanitize_event(event))
                _collect_id(event, "response_id", response_ids)
                _collect_id(event, "item_id", item_ids)

                if event_type == "error":
                    raise RealtimeAPIError(json.dumps(event.get("error", event)))

                if event_type == "input_audio_buffer.speech_started":
                    speech_started_event_ms = speech_started_event_ms or elapsed_ms
                    _collect_id(event, "item_id", item_ids)
                    continue

                if event_type == "input_audio_buffer.speech_stopped":
                    speech_stopped_event_ms = speech_stopped_event_ms or elapsed_ms
                    speech_stopped.set()
                    _collect_id(event, "item_id", item_ids)
                    continue

                if event_type == "input_audio_buffer.committed":
                    _collect_id(event, "item_id", item_ids)
                    continue

                if event_type == "conversation.item.input_audio_transcription.delta":
                    text = str(event.get("text", ""))
                    stash = str(event.get("stash", ""))
                    if text or stash:
                        input_transcript_delta = text + stash
                    continue

                if event_type == "conversation.item.input_audio_transcription.completed":
                    input_transcripts.append(str(event.get("transcript", "")))
                    continue

                if event_type == "response.created":
                    response_created_ms = response_created_ms or elapsed_ms
                    response = event.get("response", {})
                    if isinstance(response, dict):
                        _append_unique(response_ids, str(response.get("id", "")))
                    continue

                if event_type == "response.audio_transcript.delta":
                    output_transcript_parts.append(str(event.get("delta", "")))
                    continue

                if event_type == "response.audio_transcript.done":
                    transcript = str(event.get("transcript", ""))
                    if transcript:
                        output_transcript_parts = [transcript]
                    continue

                if event_type == "response.audio.delta":
                    if first_audio_delta_ms is None:
                        first_audio_delta_ms = elapsed_ms
                    output_audio.extend(base64.b64decode(event.get("delta", "")))
                    continue

                if event_type == "response.done":
                    response_done_ms = elapsed_ms
                    response = event.get("response", {})
                    if isinstance(response, dict):
                        _append_unique(response_ids, str(response.get("id", "")))
                    return

        reader_task = asyncio.create_task(
            read_events(),
            name="server-vad-probe-reader",
        )
        try:
            audio_to_send = input_pcm + _silence_pcm(
                DEFAULT_INPUT_SAMPLE_RATE,
                trailing_silence_ms,
            )
            frame_bytes = pcm_s16le_frame_bytes(DEFAULT_INPUT_SAMPLE_RATE, chunk_ms)
            for chunk in split_audio_frames(audio_to_send, frame_bytes, pad_last=True):
                if speech_stopped.is_set() or reader_task.done():
                    break
                await _send_event(
                    ws,
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                if send_delay_ms > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            speech_stopped.wait(),
                            timeout=send_delay_ms / 1000,
                        )

            await reader_task
        finally:
            if not reader_task.done():
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader_task

    return (
        ServerVadProbeResult(
            model=config.model,
            voice=config.voice,
            input_audio_bytes=len(input_pcm),
            trailing_silence_ms=trailing_silence_ms,
            output_audio_bytes=len(output_audio),
            input_transcript="".join(input_transcripts) or input_transcript_delta,
            output_transcript="".join(output_transcript_parts),
            event_counts=dict(event_counter),
            sanitized_events=sanitized_events,
            response_ids=response_ids,
            item_ids=item_ids,
            speech_started_event_ms=speech_started_event_ms,
            speech_stopped_event_ms=speech_stopped_event_ms,
            response_created_ms=response_created_ms,
            first_audio_delta_ms=first_audio_delta_ms,
            response_done_ms=response_done_ms,
        ),
        bytes(output_audio),
    )


def build_realtime_url(base_url: str, model: str) -> str:
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["model"] = model
    return urlunparse(parsed._replace(query=urlencode(query)))


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(event)
    if sanitized.get("type") == "response.audio.delta" and "delta" in sanitized:
        value = sanitized["delta"]
        if isinstance(value, str):
            sanitized["delta"] = f"<base64:{len(value)} chars>"
    if "audio" in sanitized:
        value = sanitized["audio"]
        if isinstance(value, str):
            sanitized["audio"] = f"<base64:{len(value)} chars>"
    return sanitized


async def _send_event(ws, event: dict[str, Any]) -> None:
    payload = dict(event)
    payload.setdefault("event_id", f"event_{uuid.uuid4().hex}")
    await ws.send(json.dumps(payload, ensure_ascii=False))


def _silence_pcm(sample_rate: int, duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    frame_bytes = pcm_s16le_frame_bytes(sample_rate, duration_ms)
    return b"\x00" * frame_bytes


def _collect_id(event: dict[str, Any], name: str, values: list[str]) -> None:
    value = event.get(name)
    if isinstance(value, str):
        _append_unique(values, value)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)
