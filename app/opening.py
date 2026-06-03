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
    pcm_s16le_rms,
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
from .realtime_types import RealtimeDialogConfig

LOGGER = logging.getLogger(__name__)

OPENING_TEMPLATE = (
    "您好，请问是{owner_name}吗？这边有一项物业费事项想和您本人核实一下。"
)
BUSINESS_OPENING_TEMPLATE = (
    "您好，请问是{salutation}吗？我是{employee_name}。"
    "这边有一项物业费事项需要和您本人核实一下，请问现在方便确认吗？"
)
LEGACY_BUSINESS_OPENING_TEMPLATE_ALIASES = {
    "{{name}}": "{name}",
    "{{identity_name}}": "{identity_name}",
    "{{identityName}}": "{identityName}",
}
OPENING_TTS_PREFIX = (
    "你正在进行一通电话外呼。请用自然、礼貌、像真人客服接通电话一样的口吻说出下面开场白。"
    "语速适中，语气要和后续实时对话保持一致。不要播报标点，不要像朗读通知，不要添加新事实。"
    "开场白："
)
DEFAULT_OPENING_TIMEOUT_SECONDS = 60
OPENING_LEADING_SILENCE_RMS_THRESHOLD = 120
OPENING_MAX_LEADING_SILENCE_TRIM_MS = 500
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
    speaking_style: str | None = None


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
            dialog=RealtimeDialogConfig(speaking_style=opening.speaking_style),
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


def build_business_opening_request(
    *,
    employee_name: object,
    debtor_name: object,
    debtor_gender: object,
    debt_amount: object,
    address: object,
    speaking_style: object | None = None,
    opening_template: object | None = None,
    voice: str = "female",
    speaker: str | None = None,
) -> OpeningRequest:
    if speaker is None:
        speaker = VOICE_SPEAKERS.get(voice)
    if speaker is None:
        raise OpeningGenerationFailed("opening.voice must be female or male")
    voice_text = _business_text(voice, "voice", max_length=64)
    speaker_text = _business_text(speaker, "speaker", max_length=128)

    employee_name_text = _business_text(employee_name, "employee_name", max_length=32)
    debtor_name_text = _business_text(debtor_name, "debtor_name", max_length=32)
    gender_text = "" if debtor_gender is None else str(debtor_gender).strip()
    amount_text = _arrears_amount(debt_amount)
    address_text = _business_text(address, "address", max_length=120)
    title = _debtor_title(gender_text)
    salutation = _debtor_salutation(debtor_name_text, title)
    speaking_style_text = _optional_business_text(
        speaking_style,
        "speaking_style",
        max_length=500,
    )
    business_values = {
        "employee_name": employee_name_text,
        "debtor_name": debtor_name_text,
        "debtor_gender": gender_text,
        "debt_amount": amount_text,
        "address": address_text,
        "title": title,
        "salutation": salutation,
    }
    template_values = {
        **business_values,
        "name": salutation,
        "identity_name": employee_name_text,
        "identityName": employee_name_text,
    }
    rendered = _render_business_opening_template(opening_template, template_values)
    return OpeningRequest(
        voice=voice_text,
        speaker=speaker_text,
        business=business_values,
        opening_text=rendered,
        opening_text_hash=_text_hash(rendered),
        speaking_style=speaking_style_text,
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
    phone_pcm = _trim_leading_opening_silence(
        phone_pcm,
        frame_bytes=frame_bytes,
        frame_duration_ms=config.freeswitch.frame_duration_ms,
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


def _business_text(value: object, field_name: str, *, max_length: int) -> str:
    if value is None:
        raise OpeningGenerationFailed(f"{field_name} is required")
    text = " ".join(str(value).split())
    if not text:
        raise OpeningGenerationFailed(f"{field_name} is required")
    if len(text) > max_length:
        raise OpeningGenerationFailed(f"{field_name} is too long")
    return text


def _optional_business_text(
    value: object | None,
    field_name: str,
    *,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > max_length:
        raise OpeningGenerationFailed(f"{field_name} is too long")
    return text


def _debtor_title(gender: str) -> str:
    if gender == "男":
        return "先生"
    if gender == "女":
        return "女士"
    return ""


def _debtor_salutation(debtor_name: str, title: str) -> str:
    if not title:
        return f"{debtor_name[0]}业主"
    return f"{debtor_name[0]}{title}"


def _render_business_opening_template(
    opening_template: object | None,
    values: dict[str, str],
) -> str:
    default_text = BUSINESS_OPENING_TEMPLATE.format(**values)
    if opening_template is None:
        return default_text

    template_text = " ".join(str(opening_template).split())
    if not template_text:
        return default_text
    template_text = _normalize_business_opening_template(template_text)

    try:
        rendered = template_text.format(**values)
    except (KeyError, IndexError, ValueError):
        LOGGER.warning("business_opening_template_render_failed", exc_info=True)
        return default_text

    rendered = " ".join(rendered.split())
    if _contains_pre_identity_sensitive_details(rendered, values):
        LOGGER.warning("business_opening_template_contains_sensitive_details")
        return default_text
    return rendered or default_text


def _normalize_business_opening_template(template_text: str) -> str:
    for legacy_token, format_token in LEGACY_BUSINESS_OPENING_TEMPLATE_ALIASES.items():
        template_text = template_text.replace(legacy_token, format_token)
    return template_text


def _contains_pre_identity_sensitive_details(
    rendered: str,
    values: dict[str, str],
) -> bool:
    for key in ("debtor_name", "address", "debt_amount"):
        value = values.get(key, "")
        if value and value in rendered:
            return True
    return False


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trim_leading_opening_silence(
    pcm: bytes,
    *,
    frame_bytes: int,
    frame_duration_ms: int,
) -> bytes:
    if not pcm:
        return pcm
    max_trim_frames = OPENING_MAX_LEADING_SILENCE_TRIM_MS // frame_duration_ms
    if max_trim_frames <= 0:
        return pcm
    max_trim_bytes = min(len(pcm), max_trim_frames * frame_bytes)
    offset = 0
    while offset + frame_bytes <= max_trim_bytes:
        frame = pcm[offset : offset + frame_bytes]
        if pcm_s16le_rms(frame) >= OPENING_LEADING_SILENCE_RMS_THRESHOLD:
            return pcm[offset:]
        offset += frame_bytes
    if len(pcm) > max_trim_bytes:
        return pcm[max_trim_bytes:]
    return pcm


def _tail_silence_frames(config: GatewayConfig, frame_bytes: int) -> list[bytes]:
    frame_ms = config.freeswitch.frame_duration_ms
    if config.playback.tail_silence_ms <= 0:
        return []
    if config.playback.tail_silence_ms % frame_ms != 0:
        raise OpeningGenerationFailed("playback.tail_silence_ms must align to frame")
    frame_count = config.playback.tail_silence_ms // frame_ms
    return [b"\x00" * frame_bytes for _ in range(frame_count)]
