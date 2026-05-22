from __future__ import annotations

from dataclasses import dataclass


DEFAULT_INPUT_SAMPLE_RATE = 16000
DEFAULT_OUTPUT_SAMPLE_RATE = 24000


@dataclass(frozen=True)
class RealtimeDialogContextItem:
    role: str
    text: str
    timestamp: int | None = None

    def to_payload(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "role": self.role,
            "text": self.text,
        }
        if self.timestamp is not None:
            payload["timestamp"] = self.timestamp
        return payload


@dataclass(frozen=True)
class RealtimeDialogConfig:
    bot_name: str | None = None
    system_role: str | None = None
    speaking_style: str | None = None
    model: str | None = None
    dialog_id: str | None = None
    dialog_context: tuple[RealtimeDialogContextItem, ...] = ()


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
    asr_ended_ms: int | None = None
    status: str = "completed"
    response_id: str | None = None
