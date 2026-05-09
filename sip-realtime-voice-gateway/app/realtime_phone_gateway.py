from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServer, WebSocketServerProtocol, serve

from .audio_codec import pcm_s16le_frame_bytes, resample_pcm_s16le_mono
from .config import GatewayConfig
from .realtime_client import (
    DEFAULT_INPUT_SAMPLE_RATE,
    DEFAULT_OUTPUT_SAMPLE_RATE,
    RealtimeStreamingSession,
    RealtimeTurnResult,
)
from .voice_activity import DetectedTurn, EnergyVadTurnDetector

LOGGER = logging.getLogger(__name__)

DEFAULT_PHONE_INSTRUCTIONS = (
    "You are a Chinese phone customer service assistant. "
    "Reply in short, natural spoken Chinese. "
    "Keep each reply within two short sentences."
)


@dataclass(frozen=True)
class PlaybackFrame:
    turn_id: int
    payload: bytes


class RealtimeSessionProtocol:
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def append_audio(self, input_pcm_16k: bytes) -> None: ...

    async def clear_input_buffer(self) -> None: ...

    async def commit_and_create_response(
        self,
        *,
        turn_id: int,
        input_audio_bytes: int,
    ) -> asyncio.Future[RealtimeTurnResult]: ...

    async def cancel_response(self) -> None: ...


RealtimeSessionFactory = Callable[
    [Callable[[int, bytes], Awaitable[None]]],
    RealtimeSessionProtocol,
]


@dataclass
class RealtimePhoneSessionStats:
    call_id: str
    session_id: str
    connected_at: float
    last_seen_at: float
    expected_frame_bytes: int
    inbound_frames: int = 0
    inbound_bytes: int = 0
    outbound_frames: int = 0
    outbound_bytes: int = 0
    invalid_frame_count: int = 0
    ignored_busy_frames: int = 0
    interruptions: int = 0
    dropped_playback_frames: int = 0
    dropped_stale_frames: int = 0
    cancelled_turns: int = 0
    response_cancel_events: int = 0
    turns_started: int = 0
    turns_committed: int = 0
    turns_completed: int = 0
    turns_failed: int = 0
    streamed_input_bytes: int = 0
    first_audio_at: float | None = None
    first_playback_at: float | None = None
    disconnected_at: float | None = None
    control_messages: list[str] = field(default_factory=list)
    input_transcripts: list[str] = field(default_factory=list)
    output_transcripts: list[str] = field(default_factory=list)
    current_capture_turn_id: int | None = None
    current_output_turn_id: int | None = None
    current_turn_task: asyncio.Task[None] | None = field(default=None, repr=False)
    playback_buffers: dict[int, "DownsampledPlaybackBuffer"] = field(
        default_factory=dict,
        repr=False,
    )
    playback_queue: asyncio.Queue[PlaybackFrame | None] = field(
        default_factory=asyncio.Queue,
        repr=False,
    )
    playback_active: bool = False


class FreeSwitchRealtimeGatewayServer:
    """FreeSWITCH media server that connects phone audio to a realtime model."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        api_key: str,
        instructions: str = DEFAULT_PHONE_INSTRUCTIONS,
        frame_duration_ms: int = 20,
        realtime_session_factory: RealtimeSessionFactory | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")

        self.config = config
        self.api_key = api_key
        self.instructions = instructions
        self.frame_duration_ms = frame_duration_ms
        self.expected_frame_bytes = pcm_s16le_frame_bytes(
            config.freeswitch.sample_rate,
            frame_duration_ms,
        )
        self._server: WebSocketServer | None = None
        self._address: tuple[str, int] = (
            config.freeswitch.media_host,
            config.freeswitch.media_port,
        )
        self.active_sessions: dict[str, RealtimePhoneSessionStats] = {}
        self.completed_sessions: list[RealtimePhoneSessionStats] = []
        self._realtime_session_factory = realtime_session_factory

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    async def start(self) -> None:
        self._server = await serve(
            self._handle_connection,
            self.config.freeswitch.media_host,
            self.config.freeswitch.media_port,
            ping_interval=None,
        )
        if self._server.sockets:
            sockname = self._server.sockets[0].getsockname()
            self._address = (str(sockname[0]), int(sockname[1]))

        LOGGER.info(
            "freeswitch_realtime_gateway_started host=%s port=%s "
            "phone_sample_rate=%s model_input_sample_rate=%s "
            "model_output_sample_rate=%s frame_duration_ms=%s "
            "expected_frame_bytes=%s model=%s voice=%s",
            self._address[0],
            self._address[1],
            self.config.freeswitch.sample_rate,
            DEFAULT_INPUT_SAMPLE_RATE,
            DEFAULT_OUTPUT_SAMPLE_RATE,
            self.frame_duration_ms,
            self.expected_frame_bytes,
            self.config.realtime.model,
            self.config.realtime.voice,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        LOGGER.info("freeswitch_realtime_gateway_stopped")

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    async def _handle_connection(
        self,
        websocket: WebSocketServerProtocol,
    ) -> None:
        path = websocket.path
        call_id = _call_id_from_path(path)
        if call_id is None:
            LOGGER.warning("rejecting_unsupported_realtime_media_path path=%s", path)
            await websocket.close(code=1008, reason="unsupported path")
            return

        now = time.time()
        session = RealtimePhoneSessionStats(
            call_id=call_id,
            session_id=uuid.uuid4().hex,
            connected_at=now,
            last_seen_at=now,
            expected_frame_bytes=self.expected_frame_bytes,
        )
        detector = EnergyVadTurnDetector(
            self.config.vad,
            frame_bytes=self.expected_frame_bytes,
            frame_duration_ms=self.frame_duration_ms,
        )

        realtime_session = self._create_realtime_session(session)
        playback_task = asyncio.create_task(
            self._playback_worker(websocket, session),
            name=f"playback-{session.session_id}",
        )
        self.active_sessions[session.session_id] = session

        LOGGER.info(
            "freeswitch_realtime_media_connected call_id=%s session_id=%s peer=%s",
            call_id,
            session.session_id,
            websocket.remote_address,
        )

        try:
            await realtime_session.connect()
            LOGGER.info(
                "realtime_session_connected call_id=%s session_id=%s",
                session.call_id,
                session.session_id,
            )
            async for message in websocket:
                session.last_seen_at = time.time()
                if isinstance(message, bytes):
                    await self._handle_audio_frame(
                        session,
                        detector,
                        realtime_session,
                        message,
                    )
                    continue
                await self._handle_control_message(websocket, session, message)
        except ConnectionClosed as err:
            LOGGER.info(
                "freeswitch_realtime_media_disconnected call_id=%s session_id=%s "
                "code=%s",
                call_id,
                session.session_id,
                err.code,
            )
        finally:
            await self._shutdown_session(session, playback_task, realtime_session)

    def _create_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> RealtimeSessionProtocol:
        async def on_audio_delta(turn_id: int, audio_delta_24k: bytes) -> None:
            await self._queue_audio_delta(session, turn_id, audio_delta_24k)

        if self._realtime_session_factory is not None:
            return self._realtime_session_factory(on_audio_delta)

        return RealtimeStreamingSession(
            self.config.realtime,
            api_key=self.api_key,
            instructions=self.instructions,
            on_audio_delta=on_audio_delta,
        )

    async def _handle_audio_frame(
        self,
        session: RealtimePhoneSessionStats,
        detector: EnergyVadTurnDetector,
        realtime_session: RealtimeSessionProtocol,
        payload: bytes,
    ) -> None:
        if session.first_audio_at is None:
            session.first_audio_at = time.time()
            LOGGER.info(
                "first_freeswitch_realtime_audio call_id=%s session_id=%s bytes=%s",
                session.call_id,
                session.session_id,
                len(payload),
            )

        session.inbound_frames += 1
        session.inbound_bytes += len(payload)

        if len(payload) != self.expected_frame_bytes:
            session.invalid_frame_count += 1
            LOGGER.warning(
                "freeswitch_realtime_frame_size_mismatch call_id=%s "
                "session_id=%s bytes=%s expected=%s frame=%s",
                session.call_id,
                session.session_id,
                len(payload),
                self.expected_frame_bytes,
                session.inbound_frames,
            )
            return

        self._reap_turn_task(session)
        vad_event = detector.process_frame_event(payload)

        if vad_event.started:
            if self._session_is_busy(session) and not self.config.vad.barge_in_enabled:
                detector.reset()
                session.ignored_busy_frames += 1
                LOGGER.info(
                    "realtime_phone_barge_in_ignored call_id=%s session_id=%s "
                    "reason=barge_in_disabled",
                    session.call_id,
                    session.session_id,
                )
                return
            await self._start_user_turn(session, realtime_session)

        if session.current_capture_turn_id is not None and vad_event.stream_frames:
            for frame in vad_event.stream_frames:
                await self._stream_input_frame(session, realtime_session, frame)

        if vad_event.turn is not None:
            await self._commit_user_turn(session, realtime_session, vad_event.turn)

    async def _start_user_turn(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol,
    ) -> None:
        if self._session_is_busy(session):
            await self._interrupt_for_user_speech(session, realtime_session)

        session.turns_started += 1
        turn_id = session.turns_started
        session.current_capture_turn_id = turn_id
        session.streamed_input_bytes = 0
        await realtime_session.clear_input_buffer()
        LOGGER.info(
            "realtime_phone_turn_capture_started call_id=%s session_id=%s turn=%s",
            session.call_id,
            session.session_id,
            turn_id,
        )

    async def _stream_input_frame(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol,
        frame_8k: bytes,
    ) -> None:
        frame_16k = resample_pcm_s16le_mono(
            frame_8k,
            self.config.freeswitch.sample_rate,
            DEFAULT_INPUT_SAMPLE_RATE,
        )
        await realtime_session.append_audio(frame_16k)
        session.streamed_input_bytes += len(frame_16k)

    async def _commit_user_turn(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol,
        turn: DetectedTurn,
    ) -> None:
        turn_id = session.current_capture_turn_id
        if turn_id is None:
            return

        started_at = time.monotonic()
        session.current_capture_turn_id = None
        session.current_output_turn_id = turn_id
        session.playback_buffers[turn_id] = DownsampledPlaybackBuffer(
            source_rate=DEFAULT_OUTPUT_SAMPLE_RATE,
            target_rate=self.config.freeswitch.sample_rate,
            frame_bytes=self.expected_frame_bytes,
        )

        LOGGER.info(
            "realtime_phone_turn_committed call_id=%s session_id=%s turn=%s "
            "reason=%s streamed_model_input_bytes=%s duration_ms=%s speech_ms=%s",
            session.call_id,
            session.session_id,
            turn_id,
            turn.reason,
            session.streamed_input_bytes,
            turn.duration_ms,
            turn.speech_ms,
        )

        try:
            result_future = await realtime_session.commit_and_create_response(
                turn_id=turn_id,
                input_audio_bytes=session.streamed_input_bytes,
            )
        except Exception:
            session.turns_failed += 1
            session.current_output_turn_id = None
            session.playback_buffers.pop(turn_id, None)
            LOGGER.exception(
                "realtime_phone_turn_commit_failed call_id=%s session_id=%s turn=%s",
                session.call_id,
                session.session_id,
                turn_id,
            )
            return

        session.turns_committed += 1
        session.current_turn_task = asyncio.create_task(
            self._finalize_turn(session, turn_id, started_at, result_future),
            name=f"realtime-turn-{session.session_id}-{turn_id}",
        )

    async def _finalize_turn(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
        started_at: float,
        result_future: asyncio.Future[RealtimeTurnResult],
    ) -> None:
        try:
            result = await result_future
            output_buffer = session.playback_buffers.pop(turn_id, None)
            if output_buffer is not None and session.current_output_turn_id == turn_id:
                for frame in output_buffer.flush(pad_last=True):
                    await session.playback_queue.put(PlaybackFrame(turn_id, frame))

            session.turns_completed += 1
            if result.input_transcript:
                session.input_transcripts.append(result.input_transcript)
            if result.output_transcript:
                session.output_transcripts.append(result.output_transcript)

            LOGGER.info(
                "realtime_phone_turn_completed call_id=%s session_id=%s turn=%s "
                "input_transcript=%s output_transcript=%s first_audio_delta_ms=%s "
                "response_done_ms=%s status=%s elapsed_ms=%s",
                session.call_id,
                session.session_id,
                turn_id,
                result.input_transcript,
                result.output_transcript,
                result.first_audio_delta_ms,
                result.response_done_ms,
                result.status,
                int((time.monotonic() - started_at) * 1000),
            )
        except asyncio.CancelledError:
            LOGGER.info(
                "realtime_phone_turn_cancelled call_id=%s session_id=%s turn=%s",
                session.call_id,
                session.session_id,
                turn_id,
            )
            raise
        except Exception:
            session.turns_failed += 1
            LOGGER.exception(
                "realtime_phone_turn_failed call_id=%s session_id=%s turn=%s",
                session.call_id,
                session.session_id,
                turn_id,
            )
        finally:
            if session.current_output_turn_id == turn_id and not self._has_playback(session):
                session.current_output_turn_id = None

    async def _queue_audio_delta(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
        audio_delta_24k: bytes,
    ) -> None:
        if turn_id != session.current_output_turn_id:
            session.dropped_stale_frames += 1
            return

        output_buffer = session.playback_buffers.get(turn_id)
        if output_buffer is None:
            session.dropped_stale_frames += 1
            return

        for frame in output_buffer.push(audio_delta_24k):
            await session.playback_queue.put(PlaybackFrame(turn_id, frame))

    async def _playback_worker(
        self,
        websocket: WebSocketServerProtocol,
        session: RealtimePhoneSessionStats,
    ) -> None:
        while True:
            item = await session.playback_queue.get()
            if item is None:
                return

            if item.turn_id != session.current_output_turn_id:
                session.dropped_stale_frames += 1
                continue

            try:
                session.playback_active = True
                await websocket.send(item.payload)
            except ConnectionClosed:
                session.playback_active = False
                return

            if session.first_playback_at is None:
                session.first_playback_at = time.time()
                LOGGER.info(
                    "first_realtime_phone_playback call_id=%s session_id=%s",
                    session.call_id,
                    session.session_id,
                )

            session.outbound_frames += 1
            session.outbound_bytes += len(item.payload)
            await asyncio.sleep(self.frame_duration_ms / 1000)
            session.playback_active = False

            if (
                session.current_turn_task is not None
                and session.current_turn_task.done()
                and not self._has_playback(session)
            ):
                session.current_output_turn_id = None

    async def _handle_control_message(
        self,
        websocket: WebSocketServerProtocol,
        session: RealtimePhoneSessionStats,
        raw_message: str,
    ) -> None:
        message_type = "unknown"
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw_message)
            if isinstance(payload, dict):
                message_type = str(payload.get("type", "unknown"))

        session.control_messages.append(message_type)
        LOGGER.info(
            "freeswitch_realtime_control_message call_id=%s session_id=%s type=%s",
            session.call_id,
            session.session_id,
            message_type,
        )

        if message_type == "ping":
            await websocket.send(
                json.dumps(
                    {
                        "type": "pong",
                        "call_id": session.call_id,
                        "session_id": session.session_id,
                    }
                )
            )

    async def _interrupt_for_user_speech(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol,
    ) -> None:
        session.interruptions += 1

        current_task = session.current_turn_task
        if current_task is not None and not current_task.done():
            session.cancelled_turns += 1
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await current_task
            await realtime_session.cancel_response()
            session.response_cancel_events += 1
        session.current_turn_task = None

        dropped_frames = self._clear_playback_queue(session)
        session.dropped_playback_frames += dropped_frames
        session.current_output_turn_id = None
        session.playback_buffers.clear()
        session.playback_active = False

        LOGGER.info(
            "realtime_phone_user_interrupt call_id=%s session_id=%s "
            "interruptions=%s dropped_playback_frames=%s cancelled_turns=%s "
            "response_cancel_events=%s",
            session.call_id,
            session.session_id,
            session.interruptions,
            dropped_frames,
            session.cancelled_turns,
            session.response_cancel_events,
        )

    @staticmethod
    def _clear_playback_queue(session: RealtimePhoneSessionStats) -> int:
        dropped = 0
        while True:
            try:
                item = session.playback_queue.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            if item is not None:
                dropped += 1

    async def _shutdown_session(
        self,
        session: RealtimePhoneSessionStats,
        playback_task: asyncio.Task[None],
        realtime_session: RealtimeSessionProtocol,
    ) -> None:
        current_task = session.current_turn_task
        if current_task is not None and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await current_task

        await realtime_session.close()
        await session.playback_queue.put(None)
        with contextlib.suppress(asyncio.CancelledError):
            await playback_task

        current = self.active_sessions.pop(session.session_id, None)
        if current is None:
            return

        session.disconnected_at = time.time()
        self.completed_sessions.append(session)
        LOGGER.info(
            "freeswitch_realtime_session_finished call_id=%s session_id=%s "
            "inbound_frames=%s inbound_bytes=%s outbound_frames=%s outbound_bytes=%s "
            "invalid_frame_count=%s ignored_busy_frames=%s interruptions=%s "
            "dropped_playback_frames=%s dropped_stale_frames=%s cancelled_turns=%s "
            "response_cancel_events=%s turns_started=%s turns_committed=%s "
            "turns_completed=%s turns_failed=%s duration_ms=%s",
            session.call_id,
            session.session_id,
            session.inbound_frames,
            session.inbound_bytes,
            session.outbound_frames,
            session.outbound_bytes,
            session.invalid_frame_count,
            session.ignored_busy_frames,
            session.interruptions,
            session.dropped_playback_frames,
            session.dropped_stale_frames,
            session.cancelled_turns,
            session.response_cancel_events,
            session.turns_started,
            session.turns_committed,
            session.turns_completed,
            session.turns_failed,
            int((session.disconnected_at - session.connected_at) * 1000),
        )

    def _reap_turn_task(self, session: RealtimePhoneSessionStats) -> None:
        current_task = session.current_turn_task
        if current_task is not None and current_task.done():
            session.current_turn_task = None

    def _session_is_busy(self, session: RealtimePhoneSessionStats) -> bool:
        current_task = session.current_turn_task
        if current_task is not None and not current_task.done():
            return True
        return self._has_playback(session)

    def _has_playback(self, session: RealtimePhoneSessionStats) -> bool:
        return session.playback_active or not session.playback_queue.empty()


class DownsampledPlaybackBuffer:
    def __init__(self, *, source_rate: int, target_rate: int, frame_bytes: int) -> None:
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.frame_bytes = frame_bytes
        self._source_pending = bytearray()
        self._target_pending = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._source_pending.extend(pcm)
        aligned_len = len(self._source_pending) - (len(self._source_pending) % 2)
        if aligned_len > 0:
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


def _call_id_from_path(path: str) -> str | None:
    prefixes = ("/media/fs/", "/media/")
    for prefix in prefixes:
        if not path.startswith(prefix):
            continue
        call_id = path[len(prefix) :].strip("/")
        if call_id:
            return call_id
    return None
