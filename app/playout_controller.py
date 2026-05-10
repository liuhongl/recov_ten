from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlayoutPacingMode(str, Enum):
    REALTIME = "realtime"
    FAST = "fast"


@dataclass
class PlayoutControllerConfig:
    frame_duration_ms: int
    fast_send_interval_ms: int
    prefill_frames: int
    low_watermark_frames: int | None = None
    high_watermark_frames: int | None = None


@dataclass
class PlayoutPacingState:
    mode: PlayoutPacingMode = PlayoutPacingMode.REALTIME


@dataclass(frozen=True)
class PlayoutDecision:
    interval_ms: int
    mode: PlayoutPacingMode
    switched: bool


class PlayoutController:
    """Hysteresis-based pacer for feeding FreeSWITCH playback buffers."""

    def __init__(self, config: PlayoutControllerConfig) -> None:
        if config.frame_duration_ms <= 0:
            raise ValueError("frame_duration_ms must be positive")
        if config.fast_send_interval_ms <= 0:
            raise ValueError("playback.send_interval_ms must be positive")
        if config.fast_send_interval_ms > config.frame_duration_ms:
            raise ValueError(
                "playback.send_interval_ms must be less than or equal to "
                "frame_duration_ms"
            )
        if config.prefill_frames <= 0:
            raise ValueError("prefill_frames must be positive")

        self.frame_duration_ms = config.frame_duration_ms
        self.fast_send_interval_ms = config.fast_send_interval_ms
        self.prefill_frames = config.prefill_frames
        self.low_watermark_frames = (
            config.low_watermark_frames
            if config.low_watermark_frames is not None
            else max(1, config.prefill_frames // 2)
        )
        self.high_watermark_frames = (
            config.high_watermark_frames
            if config.high_watermark_frames is not None
            else config.prefill_frames
        )
        if self.low_watermark_frames <= 0:
            raise ValueError("low_watermark_frames must be positive")
        if self.high_watermark_frames < self.low_watermark_frames:
            raise ValueError(
                "high_watermark_frames must be greater than or equal to "
                "low_watermark_frames"
            )

    def new_state(self) -> PlayoutPacingState:
        return PlayoutPacingState()

    def decide(
        self,
        state: PlayoutPacingState,
        *,
        queued_frames: int,
    ) -> PlayoutDecision:
        previous_mode = state.mode
        if self.fast_send_interval_ms >= self.frame_duration_ms:
            state.mode = PlayoutPacingMode.REALTIME
        elif state.mode == PlayoutPacingMode.FAST:
            if queued_frames <= self.low_watermark_frames:
                state.mode = PlayoutPacingMode.REALTIME
        elif queued_frames >= self.high_watermark_frames:
            state.mode = PlayoutPacingMode.FAST

        interval_ms = (
            self.fast_send_interval_ms
            if state.mode == PlayoutPacingMode.FAST
            else self.frame_duration_ms
        )
        return PlayoutDecision(
            interval_ms=interval_ms,
            mode=state.mode,
            switched=state.mode != previous_mode,
        )
