from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol

from .audio_codec import (
    pcm_s16le_frame_bytes,
    resample_pcm_s16le_mono,
    split_audio_frames,
)
from .config import DoubaoS2SConfig, GatewayConfig
from .doubao_s2s_client import (
    DoubaoS2SCredentials,
    DoubaoS2SError,
    DoubaoS2SSessionConfig,
    run_doubao_s2s_text_probe,
)

LOGGER = logging.getLogger(__name__)

OPENING_TEMPLATE = (
    "您好，请问是{owner_name}吗？系统显示您当前有{arrears_amount}元待缴费用，"
    "想和您确认一下。"
)
OPENING_TTS_PREFIX = "请严格朗读以下开场白，不要添加、删减或改写："
DEFAULT_OPENING_TIMEOUT_SECONDS = 60
VOICE_SPEAKERS = {
    "female": "zh_female_vv_jupiter_bigtts",
    "male": "zh_male_yunzhou_jupiter_bigtts",
}
OWNER_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9·._()（）-]{1,32}$")
MAX_ARREARS_AMOUNT = Decimal("9999999.99")


class OpeningGenerationFailed(ValueError):
    """Opening text or audio could not be prepared."""


class OpeningGenerationTimeout(OpeningGenerationFailed):
    """Opening audio generation exceeded the configured timeout."""


@dataclass(frozen=True)
class OpeningRequest:
    voice: str
    speaker: str
    business: dict[str, str]
    opening_text: str
    opening_text_hash: str


@dataclass(frozen=True)
class OpeningAudio:
    pcm16: bytes
    sample_rate: int
    generation_ms: int


@dataclass(frozen=True)
class OpeningCallMetadata:
    status: str
    voice: str
    speaker: str
    opening_text_hash: str
    generation_ms: int
    audio_bytes: int
    audio_sample_rate: int
    phone_frame_count: int
    call_started_after_opening_ready: bool

    def to_dict(self) -> dict[str, int | str | bool]:
        return {
            "status": self.status,
            "voice": self.voice,
            "speaker": self.speaker,
            "opening_text_hash": self.opening_text_hash,
            "generation_ms": self.generation_ms,
            "audio_bytes": self.audio_bytes,
            "audio_sample_rate": self.audio_sample_rate,
            "phone_frame_count": self.phone_frame_count,
            "call_started_after_opening_ready": (
                self.call_started_after_opening_ready
            ),
        }


@dataclass(frozen=True)
class PreparedOpeningAudio:
    call_id: str
    opening_text: str
    opening_text_hash: str
    voice: str
    speaker: str
    phone_frames: list[bytes]
    source_sample_rate: int
    source_audio_bytes: int
    generation_ms: int

    def to_call_metadata(self) -> OpeningCallMetadata:
        return OpeningCallMetadata(
            status="ready",
            voice=self.voice,
            speaker=self.speaker,
            opening_text_hash=self.opening_text_hash,
            generation_ms=self.generation_ms,
            audio_bytes=self.source_audio_bytes,
            audio_sample_rate=self.source_sample_rate,
            phone_frame_count=len(self.phone_frames),
            call_started_after_opening_ready=True,
        )


class OpeningAudioGenerator(Protocol):
    def generate(self, opening: OpeningRequest) -> OpeningAudio: ...


class OpeningAudioStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, PreparedOpeningAudio] = {}

    def put(self, audio: PreparedOpeningAudio) -> None:
        with self._lock:
            self._items[audio.call_id] = audio

    def pop(self, call_id: str) -> PreparedOpeningAudio | None:
        with self._lock:
            return self._items.pop(call_id, None)

    def discard(self, call_id: str) -> None:
        with self._lock:
            self._items.pop(call_id, None)


class DoubaoOpeningAudioGenerator:
    def __init__(
        self,
        credentials: DoubaoS2SCredentials,
        config: DoubaoS2SConfig,
        *,
        timeout_seconds: int = DEFAULT_OPENING_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.credentials = credentials
        self.config = config
        self.timeout_seconds = timeout_seconds

    def generate(self, opening: OpeningRequest) -> OpeningAudio:
        started_at = time.monotonic()
        session_config = DoubaoS2SSessionConfig(
            speaker=opening.speaker,
            output_sample_rate=self.config.output_sample_rate,
        )
        input_text = f"{OPENING_TTS_PREFIX}{opening.opening_text}"
        try:
            result, output_audio = asyncio.run(
                run_doubao_s2s_text_probe(
                    self.credentials,
                    session_config,
                    input_text=input_text,
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except TimeoutError as err:
            raise OpeningGenerationTimeout("opening_generation_timeout") from err
        except DoubaoS2SError as err:
            LOGGER.info(
                "opening_generation_failed text_hash=%s error=%s",
                opening.opening_text_hash,
                err,
            )
            raise OpeningGenerationFailed("opening_generation_failed") from err

        if not output_audio:
            raise OpeningGenerationFailed("opening_generation_failed")

        generation_ms = result.response_done_ms
        if generation_ms is None:
            generation_ms = int((time.monotonic() - started_at) * 1000)
        return OpeningAudio(
            pcm16=output_audio,
            sample_rate=result.output_sample_rate,
            generation_ms=generation_ms,
        )


def parse_opening_request(payload: object) -> OpeningRequest | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise OpeningGenerationFailed("opening must be a JSON object")

    voice = str(payload.get("voice") or "female").strip()
    speaker = VOICE_SPEAKERS.get(voice)
    if speaker is None:
        raise OpeningGenerationFailed("opening.voice must be female or male")

    business = payload.get("business")
    if not isinstance(business, dict):
        raise OpeningGenerationFailed("opening.business must be a JSON object")

    owner_name = _owner_name(business.get("owner_name"))
    arrears_amount = _arrears_amount(business.get("arrears_amount"))
    rendered = OPENING_TEMPLATE.format(
        owner_name=owner_name,
        arrears_amount=arrears_amount,
    )
    return OpeningRequest(
        voice=voice,
        speaker=speaker,
        business={
            "owner_name": owner_name,
            "arrears_amount": arrears_amount,
        },
        opening_text=rendered,
        opening_text_hash=_text_hash(rendered),
    )


def build_prepared_opening_audio(
    *,
    call_id: str,
    opening: OpeningRequest,
    audio: OpeningAudio,
    config: GatewayConfig,
) -> PreparedOpeningAudio:
    if not audio.pcm16:
        raise OpeningGenerationFailed("opening_generation_failed")
    if audio.sample_rate <= 0:
        raise OpeningGenerationFailed("opening_generation_failed")

    phone_pcm = (
        audio.pcm16
        if audio.sample_rate == config.freeswitch.sample_rate
        else resample_pcm_s16le_mono(
            audio.pcm16,
            audio.sample_rate,
            config.freeswitch.sample_rate,
        )
    )
    frame_bytes = pcm_s16le_frame_bytes(
        config.freeswitch.sample_rate,
        config.freeswitch.frame_duration_ms,
        channels=config.freeswitch.channels,
    )
    frames = split_audio_frames(phone_pcm, frame_bytes, pad_last=True)
    frames.extend(_tail_silence_frames(config, frame_bytes))
    if not frames:
        raise OpeningGenerationFailed("opening_generation_failed")

    return PreparedOpeningAudio(
        call_id=call_id,
        opening_text=opening.opening_text,
        opening_text_hash=opening.opening_text_hash,
        voice=opening.voice,
        speaker=opening.speaker,
        phone_frames=frames,
        source_sample_rate=audio.sample_rate,
        source_audio_bytes=len(audio.pcm16),
        generation_ms=audio.generation_ms,
    )


def _owner_name(value: object) -> str:
    if not isinstance(value, str):
        raise OpeningGenerationFailed("owner_name is required")
    value = value.strip()
    if not value:
        raise OpeningGenerationFailed("owner_name is required")
    if not OWNER_NAME_RE.match(value):
        raise OpeningGenerationFailed("owner_name contains unsupported characters")
    return value


def _arrears_amount(value: object) -> str:
    if value is None:
        raise OpeningGenerationFailed("arrears_amount is required")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as err:
        raise OpeningGenerationFailed("arrears_amount must be a decimal amount") from err
    if amount <= 0 or amount > MAX_ARREARS_AMOUNT:
        raise OpeningGenerationFailed("arrears_amount is out of range")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(amount, "f")


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tail_silence_frames(config: GatewayConfig, frame_bytes: int) -> list[bytes]:
    frame_ms = config.freeswitch.frame_duration_ms
    if config.playback.tail_silence_ms <= 0:
        return []
    if config.playback.tail_silence_ms % frame_ms != 0:
        raise OpeningGenerationFailed("playback.tail_silence_ms must align to frame")
    frame_count = config.playback.tail_silence_ms // frame_ms
    return [b"\x00" * frame_bytes for _ in range(frame_count)]
