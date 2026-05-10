from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .audio_codec import resample_pcm_s16le_mono
from .media_contract import PhoneMediaContract


@dataclass(frozen=True)
class PlayoutFrame:
    turn_id: int
    response_id: str
    sequence: int
    payload: bytes
    duration_ms: int
    is_tail_silence: bool = False


@dataclass
class PlayoutEngineStats:
    enqueued_frames: int = 0
    drained_frames: int = 0
    cancelled_frames: int = 0
    dropped_stale_frames: int = 0
    dropped_stale_audio_bytes: int = 0
    tail_silence_frames: int = 0
    max_queue_frames: int = 0


class PlayoutEngine:
    """Offline telephone playout frame builder.

    The engine decouples model audio arrival from telephone playout frames. It
    does not own a wall-clock sender yet; P3 will attach these frames to the
    FreeSWITCH playback channel.
    """

    def __init__(
        self,
        *,
        contract: PhoneMediaContract,
        model_output_sample_rate: int = 24000,
        tail_silence_ms: int = 300,
    ) -> None:
        contract.validate_realtime_phone_contract()
        if model_output_sample_rate <= 0:
            raise ValueError("model_output_sample_rate must be positive")
        if tail_silence_ms < 0:
            raise ValueError("tail_silence_ms must be non-negative")
        if tail_silence_ms % contract.frame_duration_ms != 0:
            raise ValueError("tail_silence_ms must align to frame_duration_ms")

        self.contract = contract
        self.model_output_sample_rate = model_output_sample_rate
        self.tail_silence_ms = tail_silence_ms
        self.stats = PlayoutEngineStats()
        self._queue: deque[PlayoutFrame] = deque()
        self._buffers: dict[tuple[int, str], _DownsampleFrameBuffer] = {}
        self._active_key: tuple[int, str] | None = None
        self._closed_keys: set[tuple[int, str]] = set()
        self._sequence = 0

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    def start_response(self, *, turn_id: int, response_id: str) -> None:
        key = self._key(turn_id, response_id)
        if key in self._closed_keys:
            raise ValueError("cannot restart a closed response")
        if self._active_key and self._active_key != key:
            self.cancel_active()

        self._active_key = key
        self._buffers.setdefault(
            key,
            _DownsampleFrameBuffer(
                source_rate=self.model_output_sample_rate,
                target_rate=self.contract.sample_rate,
                frame_bytes=self.contract.pcm_frame_bytes,
            ),
        )

    def append_audio(
        self,
        *,
        turn_id: int,
        response_id: str,
        model_pcm: bytes,
    ) -> list[PlayoutFrame]:
        key = self._key(turn_id, response_id)
        if not model_pcm:
            return []
        if self._is_stale(key):
            self.stats.dropped_stale_audio_bytes += len(model_pcm)
            return []
        if self._active_key is None:
            self.start_response(turn_id=turn_id, response_id=response_id)
        if key != self._active_key:
            self.stats.dropped_stale_audio_bytes += len(model_pcm)
            return []

        buffer = self._buffers[key]
        return self._enqueue_payloads(
            key=key,
            payloads=buffer.push(model_pcm),
            is_tail_silence=False,
        )

    def finish_response(
        self,
        *,
        turn_id: int,
        response_id: str,
    ) -> list[PlayoutFrame]:
        key = self._key(turn_id, response_id)
        if self._is_stale(key) or key != self._active_key:
            self.stats.dropped_stale_frames += 1
            return []

        buffer = self._buffers.pop(key)
        frames = self._enqueue_payloads(
            key=key,
            payloads=buffer.flush(pad_last=True),
            is_tail_silence=False,
        )
        frames.extend(self._enqueue_tail_silence(key))
        self._closed_keys.add(key)
        self._active_key = None
        return frames

    def cancel_active(self) -> int:
        if self._active_key is None:
            return 0
        return self.cancel_response(
            turn_id=self._active_key[0],
            response_id=self._active_key[1],
        )

    def cancel_response(self, *, turn_id: int, response_id: str) -> int:
        key = self._key(turn_id, response_id)
        before = len(self._queue)
        self._queue = deque(frame for frame in self._queue if frame_key(frame) != key)
        cancelled = before - len(self._queue)
        self.stats.cancelled_frames += cancelled
        self._buffers.pop(key, None)
        self._closed_keys.add(key)
        if self._active_key == key:
            self._active_key = None
        return cancelled

    def drain_frames(self, max_frames: int | None = None) -> list[PlayoutFrame]:
        if max_frames is not None and max_frames < 0:
            raise ValueError("max_frames must be non-negative")

        frames: list[PlayoutFrame] = []
        while self._queue and (max_frames is None or len(frames) < max_frames):
            frames.append(self._queue.popleft())
        self.stats.drained_frames += len(frames)
        return frames

    def _enqueue_tail_silence(
        self,
        key: tuple[int, str],
    ) -> list[PlayoutFrame]:
        frame_count = self.tail_silence_ms // self.contract.frame_duration_ms
        if frame_count == 0:
            return []

        silence = b"\x00" * self.contract.pcm_frame_bytes
        payloads = [silence] * frame_count
        frames = self._enqueue_payloads(
            key=key,
            payloads=payloads,
            is_tail_silence=True,
        )
        self.stats.tail_silence_frames += len(frames)
        return frames

    def _enqueue_payloads(
        self,
        *,
        key: tuple[int, str],
        payloads: list[bytes],
        is_tail_silence: bool,
    ) -> list[PlayoutFrame]:
        frames: list[PlayoutFrame] = []
        for payload in payloads:
            frame = PlayoutFrame(
                turn_id=key[0],
                response_id=key[1],
                sequence=self._sequence,
                payload=payload,
                duration_ms=self.contract.frame_duration_ms,
                is_tail_silence=is_tail_silence,
            )
            self._sequence += 1
            self._queue.append(frame)
            frames.append(frame)

        self.stats.enqueued_frames += len(frames)
        self.stats.max_queue_frames = max(
            self.stats.max_queue_frames,
            len(self._queue),
        )
        return frames

    def _is_stale(self, key: tuple[int, str]) -> bool:
        return key in self._closed_keys

    @staticmethod
    def _key(turn_id: int, response_id: str) -> tuple[int, str]:
        if turn_id <= 0:
            raise ValueError("turn_id must be positive")
        if not response_id:
            raise ValueError("response_id is required")
        return turn_id, response_id


def frame_key(frame: PlayoutFrame) -> tuple[int, str]:
    return frame.turn_id, frame.response_id


class _DownsampleFrameBuffer:
    def __init__(self, *, source_rate: int, target_rate: int, frame_bytes: int) -> None:
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.frame_bytes = frame_bytes
        self._source_pending = bytearray()
        self._target_pending = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._source_pending.extend(pcm)
        aligned_len = len(self._source_pending) - (len(self._source_pending) % 2)
        if aligned_len:
            source_chunk = bytes(self._source_pending[:aligned_len])
            del self._source_pending[:aligned_len]
            self._target_pending.extend(
                resample_pcm_s16le_mono(
                    source_chunk,
                    self.source_rate,
                    self.target_rate,
                )
            )
        return self._drain_frames(pad_last=False)

    def flush(self, *, pad_last: bool) -> list[bytes]:
        if self._source_pending:
            if len(self._source_pending) % 2:
                self._source_pending.append(0)
            self._target_pending.extend(
                resample_pcm_s16le_mono(
                    bytes(self._source_pending),
                    self.source_rate,
                    self.target_rate,
                )
            )
            self._source_pending.clear()
        return self._drain_frames(pad_last=pad_last)

    def _drain_frames(self, *, pad_last: bool) -> list[bytes]:
        frames: list[bytes] = []
        while len(self._target_pending) >= self.frame_bytes:
            frames.append(bytes(self._target_pending[: self.frame_bytes]))
            del self._target_pending[: self.frame_bytes]

        if pad_last and self._target_pending:
            frames.append(
                bytes(self._target_pending)
                + b"\x00" * (self.frame_bytes - len(self._target_pending))
            )
            self._target_pending.clear()

        return frames
