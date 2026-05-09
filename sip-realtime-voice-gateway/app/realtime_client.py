from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass
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


async def run_realtime_probe(
    config: RealtimeConfig,
    *,
    api_key: str,
    input_pcm: bytes,
    instructions: str,
    chunk_ms: int = DEFAULT_CHUNK_MS,
    timeout_seconds: int = 60,
    send_delay_ms: int = 5,
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
                output_audio.extend(base64.b64decode(event.get("delta", "")))
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
