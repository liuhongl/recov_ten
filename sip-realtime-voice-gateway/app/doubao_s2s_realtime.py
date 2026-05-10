from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .doubao_s2s_client import (
    ERROR_EVENTS,
    EVENT_ASR_ENDED,
    EVENT_ASR_INFO,
    EVENT_ASR_RESPONSE,
    EVENT_CHAT_ENDED,
    EVENT_CHAT_RESPONSE,
    EVENT_SESSION_STARTED,
    EVENT_SESSION_FINISHED,
    EVENT_TTS_AUDIO_DATA,
    EVENT_TTS_FINISHED,
    EVENT_TTS_SEGMENT_END,
    EVENT_TTS_STARTED,
    DoubaoS2SCredentials,
    DoubaoS2SError,
    DoubaoS2SEvent,
    DoubaoS2SRealtimeSession,
    DoubaoS2SSessionConfig,
)
from .realtime_types import RealtimeTurnResult

LOGGER = logging.getLogger(__name__)


@dataclass
class _DoubaoTurnState:
    turn_id: int
    started_at: float
    event_counts: Counter[str]
    input_audio_bytes: int = 0
    output_audio_bytes: int = 0
    first_audio_delta_ms: int | None = None
    asr_ended_ms: int | None = None
    input_transcript: str = ""
    output_transcript_parts: list[str] | None = None
    status: str = "in_progress"
    invalidated: bool = False

    def __post_init__(self) -> None:
        if self.output_transcript_parts is None:
            self.output_transcript_parts = []


class DoubaoS2SServerVadSession:
    """Doubao S2S realtime session adapted to the phone gateway.

    Doubao emits ASR, dialogue, and TTS events in one server-side session. The
    gateway already owns phone-side playout, so this adapter maps provider
    events into the same turn callbacks used by the existing realtime gateway.
    """

    restart_on_interruption = False

    def __init__(
        self,
        credentials: DoubaoS2SCredentials,
        config: DoubaoS2SSessionConfig,
        *,
        turn_id_start: int = 0,
        on_speech_started: Callable[[int], Awaitable[None]],
        on_audio_delta: Callable[[int, bytes], Awaitable[None]],
        on_turn_completed: Callable[[RealtimeTurnResult], Awaitable[None]],
    ) -> None:
        self.credentials = credentials
        self.config = config
        self.on_speech_started = on_speech_started
        self.on_audio_delta = on_audio_delta
        self.on_turn_completed = on_turn_completed
        self._session: DoubaoS2SRealtimeSession | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_turn_id = turn_id_start
        self._active_input_turn_id: int | None = None
        self._active_response_turn_id: int | None = None
        self._awaiting_response_turn_ids: deque[int] = deque()
        self._turns: dict[int, _DoubaoTurnState] = {}
        self._session_restart_lock = asyncio.Lock()
        self._pending_session_started: asyncio.Future[None] | None = None
        self._pending_session_finished: asyncio.Future[None] | None = None
        self._hot_restart_in_progress = False

    async def connect(self) -> None:
        session = DoubaoS2SRealtimeSession(self.credentials, self.config)
        await session.connect()
        await session.start_session()
        self._session = session
        self._reader_task = asyncio.create_task(
            self._read_events(),
            name="doubao-s2s-server-vad-reader",
        )

    async def close(self) -> None:
        current_task = asyncio.current_task()
        if self._reader_task is not None and self._reader_task is not current_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, DoubaoS2SError):
                await self._reader_task
            self._reader_task = None
        elif self._reader_task is current_task:
            self._reader_task = None

        if self._session is not None:
            await self._session.close()
            self._session = None

        self._turns.clear()
        self._awaiting_response_turn_ids.clear()
        self._active_input_turn_id = None
        self._active_response_turn_id = None

    async def append_audio(self, input_pcm_16k: bytes) -> None:
        if not input_pcm_16k:
            return
        async with self._session_restart_lock:
            if self._active_input_turn_id is not None:
                state = self._turns.get(self._active_input_turn_id)
                if state is not None:
                    state.input_audio_bytes += len(input_pcm_16k)
            await self._require_session().send_audio(input_pcm_16k)

    async def cancel_response(self) -> None:
        self._invalidate_active_response()

    async def handle_playback_interruption(
        self,
        *,
        interrupted_output_text: str | None = None,
    ) -> None:
        del interrupted_output_text
        async with self._session_restart_lock:
            session = self._require_session()
            self._invalidate_active_response()
            self._hot_restart_in_progress = True
            started_at = time.monotonic()
            try:
                finish_future = self._new_future()
                self._pending_session_finished = finish_future
                await session.finish_session()
                await asyncio.wait_for(finish_future, timeout=5)

                self._reset_turn_state_after_hot_restart()
                session.session_id = f"session_{uuid.uuid4().hex}"

                start_future = self._new_future()
                self._pending_session_started = start_future
                await session.send_start_session()
                await asyncio.wait_for(start_future, timeout=5)
            finally:
                self._hot_restart_in_progress = False
                self._pending_session_finished = None
                self._pending_session_started = None

        LOGGER.info(
            "doubao_s2s_hot_session_restarted elapsed_ms=%s",
            int((time.monotonic() - started_at) * 1000),
        )

    async def _read_events(self) -> None:
        try:
            while True:
                event = await self._require_session().recv_event()

                if event.error and "DialogAudioIdleTimeoutError" in event.error:
                    await self._complete_active_response_on_idle()
                    LOGGER.info("doubao_s2s_audio_idle_timeout")
                    return

                if event.error or event.event in ERROR_EVENTS:
                    raise DoubaoS2SError(event.error or str(event.payload))

                if event.event == EVENT_SESSION_STARTED:
                    self._handle_session_started(event)
                    continue

                if event.event == EVENT_SESSION_FINISHED:
                    await self._handle_session_finished(event)
                    continue

                if event.event in {EVENT_ASR_INFO, EVENT_ASR_RESPONSE}:
                    if self._hot_restart_in_progress:
                        continue
                    await self._handle_asr_event(event)
                    continue

                if event.event == EVENT_ASR_ENDED:
                    if self._hot_restart_in_progress:
                        continue
                    self._handle_asr_ended(event)
                    continue

                if event.event in {
                    EVENT_TTS_STARTED,
                    EVENT_CHAT_RESPONSE,
                    EVENT_CHAT_ENDED,
                    EVENT_TTS_SEGMENT_END,
                }:
                    self._handle_response_event(event)

                if event.audio:
                    await self._handle_audio_delta(event)

                if event.event == EVENT_TTS_FINISHED:
                    await self._handle_response_done(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("doubao_s2s_realtime_reader_failed", exc_info=True)
            raise

    async def _handle_asr_event(self, event: DoubaoS2SEvent) -> None:
        state = await self._ensure_input_turn(event)
        state.event_counts[str(event.event)] += 1
        if event.text:
            state.input_transcript = event.text

    def _handle_asr_ended(self, event: DoubaoS2SEvent) -> None:
        state = self._state_for_input_event()
        if state is None:
            return
        state.event_counts[str(event.event)] += 1
        if event.text:
            state.input_transcript = event.text
        state.asr_ended_ms = int((time.monotonic() - state.started_at) * 1000)
        self._mark_input_finished(state.turn_id)

    def _handle_response_event(self, event: DoubaoS2SEvent) -> None:
        if self._hot_restart_in_progress:
            state = self._state_for_response_event()
            if state is not None:
                state.event_counts[str(event.event)] += 1
            return

        state = self._ensure_response_turn()
        state.event_counts[str(event.event)] += 1
        if event.event == EVENT_CHAT_RESPONSE and event.text:
            assert state.output_transcript_parts is not None
            state.output_transcript_parts.append(event.text)

    async def _handle_audio_delta(self, event: DoubaoS2SEvent) -> None:
        if self._hot_restart_in_progress:
            state = self._state_for_response_event()
            if state is not None:
                state.event_counts[str(EVENT_TTS_AUDIO_DATA)] += 1
            return

        state = self._ensure_response_turn()
        state.event_counts[str(EVENT_TTS_AUDIO_DATA)] += 1
        if state.invalidated:
            return

        if state.first_audio_delta_ms is None:
            state.first_audio_delta_ms = int(
                (time.monotonic() - state.started_at) * 1000
            )
        state.output_audio_bytes += len(event.audio)
        await self.on_audio_delta(state.turn_id, event.audio)

    async def _handle_response_done(self, event: DoubaoS2SEvent) -> None:
        state = self._state_for_response_event()
        if state is None:
            return
        state.event_counts[str(event.event)] += 1
        status = "cancelled" if state.invalidated else "completed"
        state.status = status

        result = RealtimeTurnResult(
            turn_id=state.turn_id,
            input_audio_bytes=state.input_audio_bytes,
            output_audio_bytes=state.output_audio_bytes,
            input_transcript=state.input_transcript,
            output_transcript=""
            if state.invalidated
            else "".join(state.output_transcript_parts or []),
            event_counts=dict(state.event_counts),
            first_audio_delta_ms=state.first_audio_delta_ms,
            response_done_ms=int((time.monotonic() - state.started_at) * 1000),
            asr_ended_ms=state.asr_ended_ms,
            status=status,
            response_id=self._require_session().session_id,
        )
        await self.on_turn_completed(result)
        if self._active_response_turn_id == state.turn_id:
            self._active_response_turn_id = None
        self._turns.pop(state.turn_id, None)

    def _handle_session_started(self, event: DoubaoS2SEvent) -> None:
        if event.session_id:
            self._require_session().session_id = event.session_id
        self._complete_future(self._pending_session_started)

    async def _handle_session_finished(self, event: DoubaoS2SEvent) -> None:
        await self._handle_response_done(event)
        self._complete_future(self._pending_session_finished)

    async def _complete_active_response_on_idle(self) -> None:
        state = self._state_for_response_event()
        if state is None or state.output_audio_bytes <= 0:
            return
        state.event_counts["DialogAudioIdleTimeoutError"] += 1
        await self._handle_response_done(
            DoubaoS2SEvent(
                event=EVENT_TTS_FINISHED,
                session_id=self._require_session().session_id,
                connect_id=self._require_session().connect_id,
                payload={"reason": "audio_idle_timeout_fallback"},
                raw_payload=b"",
                audio=b"",
                text="",
                is_final=True,
            )
        )

    async def _ensure_input_turn(
        self,
        event: DoubaoS2SEvent,
    ) -> _DoubaoTurnState:
        state = self._state_for_input_event()
        if state is not None:
            return state

        self._invalidate_active_response()
        self._next_turn_id += 1
        turn_id = self._next_turn_id
        state = _DoubaoTurnState(
            turn_id=turn_id,
            started_at=time.monotonic(),
            event_counts=Counter(),
        )
        self._turns[turn_id] = state
        self._active_input_turn_id = turn_id
        await self.on_speech_started(turn_id)
        return state

    def _ensure_response_turn(self) -> _DoubaoTurnState:
        state = self._state_for_response_event()
        if state is not None:
            return state

        if self._awaiting_response_turn_ids:
            turn_id = self._awaiting_response_turn_ids.popleft()
        elif self._active_input_turn_id is not None:
            turn_id = self._active_input_turn_id
            self._mark_input_finished(turn_id)
        else:
            self._next_turn_id += 1
            turn_id = self._next_turn_id
            self._turns[turn_id] = _DoubaoTurnState(
                turn_id=turn_id,
                started_at=time.monotonic(),
                event_counts=Counter(),
            )

        self._active_response_turn_id = turn_id
        return self._turns[turn_id]

    def _mark_input_finished(self, turn_id: int) -> None:
        if turn_id not in self._awaiting_response_turn_ids:
            self._awaiting_response_turn_ids.append(turn_id)
        if self._active_input_turn_id == turn_id:
            self._active_input_turn_id = None

    def _state_for_input_event(self) -> _DoubaoTurnState | None:
        if self._active_input_turn_id is None:
            return None
        return self._turns.get(self._active_input_turn_id)

    def _state_for_response_event(self) -> _DoubaoTurnState | None:
        if self._active_response_turn_id is None:
            return None
        return self._turns.get(self._active_response_turn_id)

    def _invalidate_active_response(self) -> None:
        if self._active_response_turn_id is None:
            return
        state = self._turns.get(self._active_response_turn_id)
        if state is not None:
            state.invalidated = True

    def _reset_turn_state_after_hot_restart(self) -> None:
        self._turns.clear()
        self._awaiting_response_turn_ids.clear()
        self._active_input_turn_id = None
        self._active_response_turn_id = None

    @staticmethod
    def _new_future() -> asyncio.Future[None]:
        return asyncio.get_running_loop().create_future()

    @staticmethod
    def _complete_future(future: asyncio.Future[None] | None) -> None:
        if future is not None and not future.done():
            future.set_result(None)

    def _require_session(self) -> DoubaoS2SRealtimeSession:
        if self._session is None:
            raise RuntimeError("Doubao S2S realtime session is not connected")
        return self._session
