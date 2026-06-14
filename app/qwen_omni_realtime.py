from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from typing import Any

from .qwen_omni_realtime_client import (
    QwenOmniRealtimeCredentials,
    QwenOmniRealtimeError,
    QwenOmniRealtimeEvent,
    QwenOmniRealtimeSession,
    QwenOmniRealtimeSessionConfig,
    elapsed_ms,
)
from .realtime_types import RealtimeTurnResult

LOGGER = logging.getLogger(__name__)


@dataclass
class _QwenTurnState:
    turn_id: int
    started_at: float
    event_counts: Counter[str] = field(default_factory=Counter)
    input_audio_bytes: int = 0
    output_audio_bytes: int = 0
    first_audio_delta_ms: int | None = None
    input_transcript: str = ""
    output_transcript_parts: list[str] = field(default_factory=list)
    output_transcript: str = ""
    response_id: str | None = None
    status: str = "in_progress"
    invalidated: bool = False


class QwenOmniRealtimeServerVadSession:
    """Qwen Omni Realtime session adapted to the phone gateway callbacks."""

    restart_on_interruption = False

    def __init__(
        self,
        credentials: QwenOmniRealtimeCredentials,
        config: QwenOmniRealtimeSessionConfig,
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
        self._session: QwenOmniRealtimeSession | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_turn_id = turn_id_start
        self._active_turn_id: int | None = None
        self._turns: dict[int, _QwenTurnState] = {}
        self._send_lock = asyncio.Lock()
        self._pending_audio_bytes = 0
        self._provider_session_id: str | None = None

    async def connect(self) -> None:
        config = self._server_vad_config()
        session = QwenOmniRealtimeSession(self.credentials, config)
        await session.connect()
        await session.send_session_update()
        self._session = session
        self._reader_task = asyncio.create_task(
            self._read_events(),
            name="qwen-omni-realtime-server-vad-reader",
        )

    async def close(self) -> None:
        current_task = asyncio.current_task()
        if self._reader_task is not None and self._reader_task is not current_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, QwenOmniRealtimeError):
                await self._reader_task
            self._reader_task = None
        elif self._reader_task is current_task:
            self._reader_task = None

        if self._session is not None:
            await self._session.close()
            self._session = None

        self._turns.clear()
        self._active_turn_id = None
        self._pending_audio_bytes = 0

    async def append_audio(self, input_pcm_16k: bytes) -> None:
        if not input_pcm_16k:
            return
        async with self._send_lock:
            state = self._active_state()
            if state is None:
                self._pending_audio_bytes += len(input_pcm_16k)
            else:
                state.input_audio_bytes += len(input_pcm_16k)
            await self._require_session().send_audio(input_pcm_16k)

    async def cancel_response(self) -> None:
        state = self._active_state()
        if state is not None:
            state.invalidated = True
        await self._require_session().cancel_response()

    async def handle_playback_interruption(
        self,
        *,
        interrupted_output_text: str | None = None,
    ) -> None:
        del interrupted_output_text
        await self.cancel_response()

    async def seed_assistant_context(
        self,
        text: str,
        *,
        source: str = "external",
    ) -> None:
        stripped = text.strip()
        if not stripped:
            return
        LOGGER.info(
            "qwen_omni_realtime_context_seed_ignored source=%s text_chars=%s",
            source,
            len(stripped),
        )

    async def send_tts_text(self, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        LOGGER.info(
            "qwen_omni_realtime_direct_tts_ignored text_chars=%s",
            len(stripped),
        )

    async def _read_events(self) -> None:
        try:
            while True:
                event = await self._require_session().recv_event()
                if event.error:
                    raise QwenOmniRealtimeError(event.error)

                if event.type == "session.created":
                    self._handle_session_created(event)
                    continue

                if event.type == "input_audio_buffer.speech_started":
                    await self._handle_speech_started(event)
                    continue

                if event.type == "conversation.item.input_audio_transcription.completed":
                    await self._handle_input_transcript(event)
                    continue

                if event.type == "response.created":
                    self._handle_response_created(event)
                    continue

                if event.type == "response.audio_transcript.delta":
                    self._handle_output_transcript_delta(event)
                    continue

                if event.type in {"response.audio_transcript.done", "response.text.done"}:
                    self._handle_output_transcript_done(event)
                    continue

                if event.audio:
                    await self._handle_audio_delta(event)
                    continue

                if event.type == "response.done":
                    await self._handle_response_done(event)
                    continue

                self._count_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("qwen_omni_realtime_reader_failed", exc_info=True)
            raise

    def _handle_session_created(self, event: QwenOmniRealtimeEvent) -> None:
        session_payload = event.payload.get("session")
        if not isinstance(session_payload, dict):
            return
        session_id = session_payload.get("id")
        if isinstance(session_id, str):
            self._provider_session_id = session_id

    async def _handle_speech_started(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._active_state()
        if state is None:
            state = self._new_turn()
            await self.on_speech_started(state.turn_id)
        self._attach_pending_audio(state)
        state.event_counts[event.type] += 1

    async def _handle_input_transcript(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._ensure_turn()
        self._attach_pending_audio(state)
        state.event_counts[event.type] += 1
        if event.text:
            state.input_transcript = event.text
            if self.on_input_transcript is not None:
                await self.on_input_transcript(state.turn_id, event.text)

    def _handle_response_created(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._ensure_turn()
        self._attach_pending_audio(state)
        state.event_counts[event.type] += 1
        response = event.payload.get("response")
        if isinstance(response, dict):
            response_id = response.get("id")
            if isinstance(response_id, str):
                state.response_id = response_id

    def _handle_output_transcript_delta(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._ensure_turn()
        state.event_counts[event.type] += 1
        if event.text:
            state.output_transcript_parts.append(event.text)

    def _handle_output_transcript_done(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._ensure_turn()
        state.event_counts[event.type] += 1
        if event.text:
            state.output_transcript = event.text

    async def _handle_audio_delta(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._ensure_turn()
        state.event_counts[event.type] += 1
        if state.invalidated:
            return
        if state.first_audio_delta_ms is None:
            state.first_audio_delta_ms = elapsed_ms(state.started_at)
        state.output_audio_bytes += len(event.audio)
        await self.on_audio_delta(state.turn_id, event.audio)

    async def _handle_response_done(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._ensure_turn()
        state.event_counts[event.type] += 1
        response = event.payload.get("response")
        if isinstance(response, dict):
            response_id = response.get("id")
            if isinstance(response_id, str):
                state.response_id = response_id

        status = "cancelled" if state.invalidated else "completed"
        state.status = status
        output_transcript = state.output_transcript
        if not output_transcript and state.output_transcript_parts:
            output_transcript = "".join(state.output_transcript_parts)
        result = RealtimeTurnResult(
            turn_id=state.turn_id,
            input_audio_bytes=state.input_audio_bytes,
            output_audio_bytes=state.output_audio_bytes,
            input_transcript=state.input_transcript,
            output_transcript=output_transcript,
            event_counts=dict(state.event_counts),
            first_audio_delta_ms=state.first_audio_delta_ms,
            response_done_ms=elapsed_ms(state.started_at),
            status=status,
            response_id=state.response_id or self._provider_session_id,
        )
        await self.on_turn_completed(result)
        if self._active_turn_id == state.turn_id:
            self._active_turn_id = None
        self._turns.pop(state.turn_id, None)

    def _count_event(self, event: QwenOmniRealtimeEvent) -> None:
        state = self._active_state()
        if state is not None:
            state.event_counts[event.type] += 1

    def _new_turn(self) -> _QwenTurnState:
        self._next_turn_id += 1
        state = _QwenTurnState(
            turn_id=self._next_turn_id,
            started_at=time.monotonic(),
        )
        self._turns[state.turn_id] = state
        self._active_turn_id = state.turn_id
        return state

    def _ensure_turn(self) -> _QwenTurnState:
        state = self._active_state()
        if state is not None:
            return state
        state = self._new_turn()
        self._attach_pending_audio(state)
        return state

    def _attach_pending_audio(self, state: _QwenTurnState) -> None:
        if self._pending_audio_bytes <= 0:
            return
        state.input_audio_bytes += self._pending_audio_bytes
        self._pending_audio_bytes = 0

    def _active_state(self) -> _QwenTurnState | None:
        if self._active_turn_id is None:
            return None
        return self._turns.get(self._active_turn_id)

    def _require_session(self) -> QwenOmniRealtimeSession:
        if self._session is None:
            raise RuntimeError("Qwen Omni Realtime session is not connected")
        return self._session

    def _server_vad_config(self) -> QwenOmniRealtimeSessionConfig:
        kwargs: dict[str, Any] = {
            config_field.name: getattr(self.config, config_field.name)
            for config_field in fields(self.config)
        }
        kwargs["manual_turn_detection"] = False
        return QwenOmniRealtimeSessionConfig(**kwargs)
