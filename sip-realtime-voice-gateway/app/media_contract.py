from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .audio_codec import SAMPLE_WIDTH_BYTES, pcm_s16le_frame_bytes


class FreeSwitchMediaConfig(Protocol):
    sample_rate: int
    phone_codec: str
    channels: int
    frame_duration_ms: int


@dataclass(frozen=True)
class PhoneMediaContract:
    codec: str
    sample_rate: int
    channels: int
    frame_duration_ms: int
    pcm_sample_width_bytes: int = SAMPLE_WIDTH_BYTES

    @classmethod
    def from_config(
        cls,
        config: FreeSwitchMediaConfig,
        *,
        frame_duration_ms: int | None = None,
    ) -> "PhoneMediaContract":
        return cls(
            codec=config.phone_codec.upper(),
            sample_rate=config.sample_rate,
            channels=config.channels,
            frame_duration_ms=(
                frame_duration_ms
                if frame_duration_ms is not None
                else config.frame_duration_ms
            ),
        )

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate * self.frame_duration_ms // 1000

    @property
    def pcm_frame_bytes(self) -> int:
        return pcm_s16le_frame_bytes(
            self.sample_rate,
            self.frame_duration_ms,
            channels=self.channels,
        )

    @property
    def encoded_payload_bytes(self) -> int:
        if self.codec in {"PCMA", "PCMU"}:
            return self.samples_per_frame * self.channels
        raise ValueError(f"unsupported phone codec: {self.codec}")

    def validate_realtime_phone_contract(self) -> None:
        if self.codec != "PCMA":
            raise ValueError("FreeSWITCH media contract requires phone_codec=PCMA")
        if self.sample_rate != 8000:
            raise ValueError("FreeSWITCH media contract requires sample_rate=8000")
        if self.channels != 1:
            raise ValueError("FreeSWITCH media contract requires channels=1")
        if self.frame_duration_ms != 20:
            raise ValueError(
                "FreeSWITCH media contract requires frame_duration_ms=20"
            )
        if self.pcm_sample_width_bytes != SAMPLE_WIDTH_BYTES:
            raise ValueError("FreeSWITCH media contract requires 16-bit PCM")

        # Force whole-sample validation and keep the error close to startup.
        _ = self.pcm_frame_bytes
        _ = self.encoded_payload_bytes

    def log_fields(self) -> dict[str, int | str]:
        return {
            "phone_codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frame_duration_ms": self.frame_duration_ms,
            "pcm_frame_bytes": self.pcm_frame_bytes,
            "encoded_payload_bytes": self.encoded_payload_bytes,
        }


def build_realtime_phone_contract(
    config: FreeSwitchMediaConfig,
) -> PhoneMediaContract:
    contract = PhoneMediaContract.from_config(config)
    contract.validate_realtime_phone_contract()
    return contract
