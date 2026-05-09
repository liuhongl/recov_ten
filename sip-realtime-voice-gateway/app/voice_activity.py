from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .audio_codec import pcm_s16le_rms
from .config import VadConfig


@dataclass(frozen=True)
class DetectedTurn:
    pcm: bytes
    reason: str
    duration_ms: int
    speech_ms: int


@dataclass(frozen=True)
class VadFrameEvent:
    started: bool = False
    stream_frames: tuple[bytes, ...] = ()
    turn: DetectedTurn | None = None


class EnergyVadTurnDetector:
    """Small deterministic VAD for finding telephone utterance boundaries."""

    def __init__(
        self,
        config: VadConfig,
        *,
        frame_bytes: int,
        frame_duration_ms: int,
    ) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        if frame_duration_ms <= 0:
            raise ValueError("frame_duration_ms must be positive")

        self.config = config
        self.frame_bytes = frame_bytes
        self.frame_duration_ms = frame_duration_ms
        self.start_frames = _ms_to_frames(config.start_speech_ms, frame_duration_ms, 1)
        self.end_silence_frames = _ms_to_frames(
            config.end_silence_ms,
            frame_duration_ms,
            1,
        )
        self.min_speech_frames = _ms_to_frames(
            config.min_speech_ms,
            frame_duration_ms,
            1,
        )
        self.max_utterance_frames = _ms_to_frames(
            config.max_utterance_ms,
            frame_duration_ms,
            1,
        )
        self.pre_speech_frames = _ms_to_frames(
            config.pre_speech_ms,
            frame_duration_ms,
            0,
        )
        self.keep_silence_frames = _ms_to_frames(
            config.keep_silence_ms,
            frame_duration_ms,
            0,
        )
        self.reset()

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._active = False
        self._start_counter = 0
        self._silence_frames = 0
        self._speech_frames = 0
        self._total_frames = 0
        self._buffer = bytearray()
        self._pre_speech: deque[bytes] = deque(maxlen=self.pre_speech_frames)

    def process_frame(self, frame: bytes) -> DetectedTurn | None:
        return self.process_frame_event(frame).turn

    def process_frame_event(self, frame: bytes) -> VadFrameEvent:
        if len(frame) != self.frame_bytes:
            raise ValueError("frame byte length does not match detector frame size")

        voiced = pcm_s16le_rms(frame) >= self.config.speech_rms_threshold
        if self._active:
            return self._process_active_frame(frame, voiced)
        return self._process_idle_frame(frame, voiced)

    def finish_if_active(self, *, reason: str = "connection_closed") -> DetectedTurn | None:
        if not self._active or self._speech_frames < self.min_speech_frames:
            self.reset()
            return None
        return self._finish(reason)

    def _process_idle_frame(self, frame: bytes, voiced: bool) -> VadFrameEvent:
        if self.pre_speech_frames > 0:
            self._pre_speech.append(frame)

        if voiced:
            self._start_counter += 1
        else:
            self._start_counter = 0

        if self._start_counter < self.start_frames:
            return VadFrameEvent()

        self._active = True
        if self._pre_speech:
            stream_frames = tuple(self._pre_speech)
            self._buffer.extend(b"".join(stream_frames))
            self._total_frames = len(self._pre_speech)
        else:
            stream_frames = (frame,)
            self._buffer.extend(frame)
            self._total_frames = 1
        self._speech_frames = min(self._start_counter, self._total_frames)
        self._silence_frames = 0
        self._pre_speech.clear()
        return VadFrameEvent(started=True, stream_frames=stream_frames)

    def _process_active_frame(self, frame: bytes, voiced: bool) -> VadFrameEvent:
        self._buffer.extend(frame)
        self._total_frames += 1

        if voiced:
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        if self._total_frames >= self.max_utterance_frames:
            return VadFrameEvent(stream_frames=(frame,), turn=self._finish("max_utterance"))

        if (
            self._speech_frames >= self.min_speech_frames
            and self._silence_frames >= self.end_silence_frames
        ):
            return VadFrameEvent(stream_frames=(frame,), turn=self._finish("silence"))

        return VadFrameEvent(stream_frames=(frame,))

    def _finish(self, reason: str) -> DetectedTurn:
        trim_frames = max(0, self._silence_frames - self.keep_silence_frames)
        trim_bytes = trim_frames * self.frame_bytes
        if trim_bytes:
            pcm = bytes(self._buffer[:-trim_bytes])
        else:
            pcm = bytes(self._buffer)

        duration_frames = len(pcm) // self.frame_bytes
        turn = DetectedTurn(
            pcm=pcm,
            reason=reason,
            duration_ms=duration_frames * self.frame_duration_ms,
            speech_ms=self._speech_frames * self.frame_duration_ms,
        )
        self.reset()
        return turn


def _ms_to_frames(ms: int, frame_duration_ms: int, minimum: int) -> int:
    if ms <= 0:
        return minimum
    frames = (ms + frame_duration_ms - 1) // frame_duration_ms
    return max(minimum, frames)
