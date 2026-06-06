from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
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
POST_CONTEXT_SEED_DIAGNOSTIC_WINDOW_MS = 15000
LATE_CONTEXT_SEED_SUPPRESSION_WINDOW_MS = 15000
PROVIDER_RESPONSE_EVENTS = {
    EVENT_TTS_STARTED,
    EVENT_CHAT_RESPONSE,
    EVENT_CHAT_ENDED,
    EVENT_TTS_SEGMENT_END,
    EVENT_TTS_AUDIO_DATA,
    EVENT_TTS_FINISHED,
}


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
    notified_input_transcript: str = ""
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

    restart_on_interruption = True

    def __init__(
        self,
        credentials: DoubaoS2SCredentials,
        config: DoubaoS2SSessionConfig,
        *,
        turn_id_start: int = 0,
        on_speech_started: Callable[[int], Awaitable[None]],
        on_audio_delta: Callable[[int, bytes], Awaitable[None]],
        on_turn_completed: Callable[[RealtimeTurnResult], Awaitable[None]],
        on_input_transcript: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        self.credentials = credentials
        self.config = config
        self.on_speech_started = on_speech_started
        self.on_input_transcript = on_input_transcript
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
        self._pending_context_seed_finished: asyncio.Future[None] | None = None
        self._pending_direct_tts_finished: asyncio.Future[None] | None = None
        self._hot_restart_in_progress = False
        self._context_seed_in_progress = False
        self._context_seed_seq = 0
        self._context_seed_id: str | None = None
        self._context_seed_source: str | None = None
        self._context_seed_text_hash: str | None = None
        self._context_seed_started_at: float | None = None
        self._last_context_seed_id: str | None = None
        self._last_context_seed_source: str | None = None
        self._last_context_seed_text_hash: str | None = None
        self._last_context_seed_outcome: str | None = None
        self._last_context_seed_finished_at: float | None = None
        self._late_context_seed_suppression_active = False
        self._late_context_seed_suppression_started_at: float | None = None
        self._late_context_seed_suppression_id: str | None = None
        self._late_context_seed_suppression_source: str | None = None
        self._late_context_seed_suppression_text_hash: str | None = None
        self._late_context_seed_suppression_outcome: str | None = None

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
            await session.client_interrupt()

    async def seed_assistant_context(
        self,
        text: str,
        *,
        source: str = "external",
    ) -> None:
        text = text.strip()
        if not text:
            return
        async with self._session_restart_lock:
            await self._seed_assistant_context_locked(text, source=source)

    async def send_tts_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        async with self._session_restart_lock:
            session = self._require_session()
            finished = self._new_future()
            self._pending_direct_tts_finished = finished
            try:
                await session.send_tts_text(text)
                await asyncio.wait_for(finished, timeout=8)
            finally:
                if self._pending_direct_tts_finished is finished:
                    self._pending_direct_tts_finished = None

    async def _seed_assistant_context_locked(
        self,
        text: str,
        *,
        source: str,
    ) -> None:
        session = self._require_session()
        self._context_seed_seq += 1
        seed_id = f"seed-{self._context_seed_seq}"
        text_hash = _text_hash(text)
        self._context_seed_in_progress = True
        started_at = time.monotonic()
        seed_future = self._new_future()
        self._pending_context_seed_finished = seed_future
        self._context_seed_id = seed_id
        self._context_seed_source = source
        self._context_seed_text_hash = text_hash
        self._context_seed_started_at = started_at
        outcome = "unknown"
        LOGGER.info(
            "doubao_s2s_context_seed_started seed_id=%s source=%s "
            "text_hash=%s text_chars=%s session_id=%s",
            seed_id,
            source,
            text_hash,
            len(text),
            session.session_id,
        )
        try:
            await session.say_hello(text)
            await asyncio.wait_for(seed_future, timeout=8)
            outcome = "completed"
        except asyncio.TimeoutError:
            outcome = "timeout"
            LOGGER.warning(
                "doubao_s2s_context_seed_failed seed_id=%s source=%s "
                "outcome=%s elapsed_ms=%s pending_done=%s "
                "pending_cancelled=%s session_id=%s",
                seed_id,
                source,
                outcome,
                int((time.monotonic() - started_at) * 1000),
                seed_future.done(),
                seed_future.cancelled(),
                session.session_id,
            )
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            LOGGER.warning(
                "doubao_s2s_context_seed_failed seed_id=%s source=%s "
                "outcome=%s elapsed_ms=%s pending_done=%s "
                "pending_cancelled=%s session_id=%s",
                seed_id,
                source,
                outcome,
                int((time.monotonic() - started_at) * 1000),
                seed_future.done(),
                seed_future.cancelled(),
                session.session_id,
            )
            raise
        except Exception:
            outcome = "error"
            LOGGER.warning(
                "doubao_s2s_context_seed_failed seed_id=%s source=%s "
                "outcome=%s elapsed_ms=%s pending_done=%s "
                "pending_cancelled=%s session_id=%s",
                seed_id,
                source,
                outcome,
                int((time.monotonic() - started_at) * 1000),
                seed_future.done(),
                seed_future.cancelled(),
                session.session_id,
                exc_info=True,
            )
            raise
        finally:
            finished_at = time.monotonic()
            self._last_context_seed_id = seed_id
            self._last_context_seed_source = source
            self._last_context_seed_text_hash = text_hash
            self._last_context_seed_outcome = outcome
            self._last_context_seed_finished_at = finished_at
            if outcome == "completed":
                self._clear_late_context_seed_suppression()
            else:
                self._late_context_seed_suppression_active = True
                self._late_context_seed_suppression_started_at = finished_at
                self._late_context_seed_suppression_id = seed_id
                self._late_context_seed_suppression_source = source
                self._late_context_seed_suppression_text_hash = text_hash
                self._late_context_seed_suppression_outcome = outcome
            self._context_seed_in_progress = False
            self._pending_context_seed_finished = None
            self._context_seed_id = None
            self._context_seed_source = None
            self._context_seed_text_hash = None
            self._context_seed_started_at = None
        LOGGER.info(
            "doubao_s2s_assistant_context_seeded seed_id=%s source=%s "
            "text_hash=%s elapsed_ms=%s",
            seed_id,
            source,
            text_hash,
            int((time.monotonic() - started_at) * 1000),
        )

    async def _read_events(self) -> None:
        try:
            while True:
                session = self._session
                if session is None:
                    return
                event = await session.recv_event()

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

                if self._context_seed_in_progress:
                    self._handle_context_seed_event(event)
                    continue

                self._log_post_context_seed_event(event)
                if self._suppress_late_context_seed_event(event):
                    continue

                if event.event in {EVENT_ASR_INFO, EVENT_ASR_RESPONSE}:
                    if self._hot_restart_in_progress:
                        continue
                    await self._handle_asr_event(event)
                    continue

                if event.event == EVENT_ASR_ENDED:
                    if self._hot_restart_in_progress:
                        continue
                    await self._handle_asr_ended(event)
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
        if event.is_final:
            await self._notify_input_transcript(state)

    async def _handle_asr_ended(self, event: DoubaoS2SEvent) -> None:
        state = self._state_for_input_event()
        if state is None:
            return
        state.event_counts[str(event.event)] += 1
        if event.text:
            state.input_transcript = event.text
        state.asr_ended_ms = int((time.monotonic() - state.started_at) * 1000)
        await self._notify_input_transcript(state)
        self._mark_input_finished(state.turn_id)

    async def _notify_input_transcript(self, state: _DoubaoTurnState) -> None:
        if self.on_input_transcript is None:
            return
        transcript = state.input_transcript.strip()
        if not transcript or transcript == state.notified_input_transcript:
            return
        state.notified_input_transcript = transcript
        await self.on_input_transcript(state.turn_id, transcript)

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
            output_transcript="".join(state.output_transcript_parts or []),
            event_counts=dict(state.event_counts),
            first_audio_delta_ms=state.first_audio_delta_ms,
            response_done_ms=int((time.monotonic() - state.started_at) * 1000),
            asr_ended_ms=state.asr_ended_ms,
            status=status,
            response_id=self._require_session().session_id,
        )
        await self.on_turn_completed(result)
        self._complete_future(self._pending_direct_tts_finished)
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

    def _handle_context_seed_event(self, event: DoubaoS2SEvent) -> None:
        LOGGER.info(
            "doubao_s2s_context_seed_event seed_id=%s source=%s "
            "text_hash=%s event=%s audio_bytes=%s text_chars=%s "
            "is_final=%s session_id=%s",
            self._context_seed_id,
            self._context_seed_source,
            self._context_seed_text_hash,
            event.event,
            len(event.audio),
            len(event.text or ""),
            event.is_final,
            event.session_id,
        )
        if event.event == EVENT_TTS_FINISHED:
            self._complete_future(self._pending_context_seed_finished)

    def _log_post_context_seed_event(self, event: DoubaoS2SEvent) -> None:
        if event.event not in PROVIDER_RESPONSE_EVENTS and not event.audio:
            return
        age_ms = _elapsed_ms(self._last_context_seed_finished_at)
        if (
            age_ms is None
            or age_ms > POST_CONTEXT_SEED_DIAGNOSTIC_WINDOW_MS
        ):
            return

        log = LOGGER.warning
        if self._last_context_seed_outcome == "completed":
            log = LOGGER.info
        log(
            "doubao_s2s_post_context_seed_event seed_id=%s source=%s "
            "text_hash=%s seed_outcome=%s age_ms=%s event=%s "
            "audio_bytes=%s text_chars=%s active_input_turn=%s "
            "active_response_turn=%s awaiting_turns=%s hot_restart=%s "
            "session_id=%s",
            self._last_context_seed_id,
            self._last_context_seed_source,
            self._last_context_seed_text_hash,
            self._last_context_seed_outcome,
            age_ms,
            event.event,
            len(event.audio),
            len(event.text or ""),
            self._active_input_turn_id,
            self._active_response_turn_id,
            list(self._awaiting_response_turn_ids),
            self._hot_restart_in_progress,
            event.session_id,
        )

    def _suppress_late_context_seed_event(self, event: DoubaoS2SEvent) -> bool:
        if not self._late_context_seed_suppression_active:
            return False

        age_ms = _elapsed_ms(self._late_context_seed_suppression_started_at)
        if (
            age_ms is not None
            and age_ms > LATE_CONTEXT_SEED_SUPPRESSION_WINDOW_MS
        ):
            LOGGER.warning(
                "doubao_s2s_late_context_seed_suppression_expired "
                "seed_id=%s source=%s text_hash=%s outcome=%s age_ms=%s "
                "event=%s audio_bytes=%s text_chars=%s session_id=%s",
                self._late_context_seed_suppression_id,
                self._late_context_seed_suppression_source,
                self._late_context_seed_suppression_text_hash,
                self._late_context_seed_suppression_outcome,
                age_ms,
                event.event,
                len(event.audio),
                len(event.text or ""),
                event.session_id,
            )
            self._clear_late_context_seed_suppression()
            return False

        if event.event in {EVENT_ASR_INFO, EVENT_ASR_RESPONSE, EVENT_ASR_ENDED}:
            LOGGER.info(
                "doubao_s2s_late_context_seed_suppression_released_by_asr "
                "seed_id=%s source=%s text_hash=%s outcome=%s age_ms=%s "
                "event=%s session_id=%s",
                self._late_context_seed_suppression_id,
                self._late_context_seed_suppression_source,
                self._late_context_seed_suppression_text_hash,
                self._late_context_seed_suppression_outcome,
                age_ms,
                event.event,
                event.session_id,
            )
            self._clear_late_context_seed_suppression()
            return False

        if event.event not in PROVIDER_RESPONSE_EVENTS and not event.audio:
            return False

        LOGGER.warning(
            "doubao_s2s_late_context_seed_event_suppressed seed_id=%s "
            "source=%s text_hash=%s outcome=%s age_ms=%s event=%s "
            "audio_bytes=%s text_chars=%s session_id=%s",
            self._late_context_seed_suppression_id,
            self._late_context_seed_suppression_source,
            self._late_context_seed_suppression_text_hash,
            self._late_context_seed_suppression_outcome,
            age_ms,
            event.event,
            len(event.audio),
            len(event.text or ""),
            event.session_id,
        )
        if event.event == EVENT_TTS_FINISHED:
            self._clear_late_context_seed_suppression()
        return True

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

        while self._awaiting_response_turn_ids:
            turn_id = self._awaiting_response_turn_ids.popleft()
            state = self._turns.get(turn_id)
            if state is not None:
                self._active_response_turn_id = turn_id
                return state
            LOGGER.warning(
                "doubao_s2s_stale_awaiting_response_turn_skipped turn_id=%s",
                turn_id,
            )

        if self._active_input_turn_id is not None:
            turn_id = self._active_input_turn_id
            state = self._turns.get(turn_id)
            self._active_input_turn_id = None
            if state is not None:
                self._active_response_turn_id = turn_id
                return state
            LOGGER.warning(
                "doubao_s2s_active_input_turn_missing_for_response turn_id=%s",
                turn_id,
            )
            self._turns[turn_id] = _DoubaoTurnState(
                turn_id=turn_id,
                started_at=time.monotonic(),
                event_counts=Counter(),
            )
        else:
            self._next_turn_id += 1
            turn_id = self._next_turn_id
            self._turns[turn_id] = _DoubaoTurnState(
                turn_id=turn_id,
                started_at=time.monotonic(),
                event_counts=Counter(),
            )
            LOGGER.warning(
                "doubao_s2s_response_turn_without_input_started turn_id=%s "
                "post_seed_id=%s post_seed_source=%s post_seed_outcome=%s "
                "post_seed_age_ms=%s active_input_turn=%s awaiting_turns=%s",
                turn_id,
                self._last_context_seed_id,
                self._last_context_seed_source,
                self._last_context_seed_outcome,
                _elapsed_ms(self._last_context_seed_finished_at),
                self._active_input_turn_id,
                list(self._awaiting_response_turn_ids),
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

    def _clear_late_context_seed_suppression(self) -> None:
        self._late_context_seed_suppression_active = False
        self._late_context_seed_suppression_started_at = None
        self._late_context_seed_suppression_id = None
        self._late_context_seed_suppression_source = None
        self._late_context_seed_suppression_text_hash = None
        self._late_context_seed_suppression_outcome = None

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


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _elapsed_ms(start: float | None) -> int | None:
    if start is None:
        return None
    return int((time.monotonic() - start) * 1000)
