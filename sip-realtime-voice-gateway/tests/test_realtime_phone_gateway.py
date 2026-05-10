from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from websockets.legacy.client import connect

from app.audio_codec import samples_to_pcm_s16le
from app.config import FreeSwitchConfig, GatewayConfig, PlaybackConfig, VadConfig
from app.freeswitch_event_socket import PlaybackProgressEvent
from app.realtime_phone_gateway import (
    FreeSwitchRealtimeGatewayServer,
    PlaybackFrame,
    RealtimePhoneSessionStats,
)
from app.realtime_types import RealtimeTurnResult


def test_realtime_phone_gateway_plays_model_audio_back_to_client():
    asyncio.run(_assert_realtime_phone_gateway_roundtrip())


def test_realtime_phone_gateway_clears_playback_on_user_interrupt():
    asyncio.run(_assert_realtime_phone_gateway_interrupts_playback())


def test_realtime_phone_gateway_replays_interrupt_audio_after_hot_restart():
    asyncio.run(_assert_realtime_phone_gateway_replays_after_hot_restart())


def test_realtime_phone_gateway_appends_tail_silence_after_turn_done():
    asyncio.run(_assert_realtime_phone_gateway_appends_tail_silence())


def test_realtime_phone_gateway_commits_after_freeswitch_playback_done():
    asyncio.run(_assert_realtime_phone_gateway_waits_for_freeswitch_completion())


def test_realtime_phone_gateway_rejects_slow_playback_send_interval():
    with pytest.raises(ValueError, match="playback.send_interval_ms"):
        FreeSwitchRealtimeGatewayServer(
            _test_config(tail_silence_ms=0, send_interval_ms=40),
            api_key="test-key",
        )


def test_realtime_phone_gateway_does_not_emit_silence_when_model_audio_lags():
    asyncio.run(_assert_realtime_phone_gateway_does_not_emit_silence_on_lag())


async def _assert_realtime_phone_gateway_roundtrip() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-realtime-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)
            assert len(playback) == 320
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert fake_session.connected is True
    assert fake_session.closed is True
    assert fake_session.appended_bytes == 1920
    assert fake_session.speech_started_turns == [1]
    assert len(server.completed_sessions) == 1
    stats = server.completed_sessions[0]
    assert stats.turns_started == 1
    assert stats.turns_committed == 1
    assert stats.turns_completed == 1
    assert stats.turns_failed == 0
    assert stats.outbound_frames == 1
    assert stats.flushed_tail_frames == 1
    assert stats.output_transcripts == ["hello from model"]
    assert stats.gateway_history_committed_turns == 1
    assert stats.gateway_history_abandoned_turns == 0
    assert [item.output_transcript for item in stats.committed_exchanges] == [
        "hello from model"
    ]


async def _assert_realtime_phone_gateway_interrupts_playback() -> None:
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 480 * 20),
        reconnect_delay_seconds=0.05,
    )
    fake_playback_control = FakePlaybackControl()
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=fake_playback_control,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-interrupt-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)

            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.interruptions == 1
    assert stats.dropped_playback_frames > 0
    assert stats.dropped_stale_frames == 0
    assert stats.freeswitch_break_requests == 1
    assert stats.freeswitch_break_failures == 0
    assert stats.realtime_interrupt_requests == 1
    assert stats.realtime_interrupt_failures == 0
    assert stats.context_repair_requests == 1
    assert stats.realtime_session_restarts == 1
    assert stats.gateway_history_committed_turns == 0
    assert stats.gateway_history_abandoned_turns == 1
    assert stats.replayed_input_frames > 0
    assert fake_playback_control.break_calls == ["test-interrupt-call"]
    assert fake_session.cancel_calls == 1
    assert fake_session.connect_calls == 2
    assert fake_session.turn_id_starts == [0, 2]
    assert fake_session.close_calls >= 1
    assert any(size > 640 for size in fake_session.append_sizes)


async def _assert_realtime_phone_gateway_replays_after_hot_restart() -> None:
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 480 * 20),
        restart_on_interruption=False,
    )
    fake_playback_control = FakePlaybackControl()
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=fake_playback_control,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-hot-restart-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)

            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.interruptions == 1
    assert stats.realtime_session_restarts == 0
    assert stats.replayed_input_frames > 0
    assert stats.replayed_input_bytes > 0
    assert fake_session.interruption_calls == ["hello from model"]
    assert fake_session.connect_calls == 1
    assert any(size > 640 for size in fake_session.append_sizes)


async def _assert_realtime_phone_gateway_appends_tail_silence() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=40),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-tail-silence-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback_frames = [
                await asyncio.wait_for(ws.recv(), timeout=3) for _ in range(3)
            ]
            assert all(isinstance(frame, bytes) for frame in playback_frames)
            assert playback_frames[1:] == [b"\x00" * 320, b"\x00" * 320]
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.outbound_frames == 3
    assert stats.flushed_tail_frames == 1
    assert stats.tail_silence_frames == 2
    assert stats.gateway_history_committed_turns == 1


async def _assert_realtime_phone_gateway_waits_for_freeswitch_completion() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=FakePlaybackControl(),
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-playback-complete-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)
            await asyncio.sleep(0.05)

            session = next(iter(server.active_sessions.values()))
            assert session.gateway_history_committed_turns == 0

            await server._handle_freeswitch_playback_event(
                PlaybackProgressEvent(
                    uuid="test-playback-complete-call",
                    event="queue_completed",
                    total_chunks=1,
                )
            )
            assert session.gateway_history_committed_turns == 1
    finally:
        await server.stop()


async def _assert_realtime_phone_gateway_does_not_emit_silence_on_lag() -> None:
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0, send_interval_ms=10),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_output_turn_id = 1
    websocket = RecordingWebSocket()

    await server._send_playback_frame(
        websocket,
        session,
        PlaybackFrame(1, b"\x01" * 320),
    )

    assert websocket.sent == [b"\x01" * 320]
    assert session.playback_underruns == 1
    assert session.playback_realtime_send_frames == 1
    assert session.playback_fast_send_frames == 0
    assert session.playback_queue.empty()


class FakePlaybackControl:
    def __init__(self) -> None:
        self.break_calls: list[str] = []

    async def break_playback(self, media_uuid: str) -> bool:
        self.break_calls.append(media_uuid)
        return True


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)


class FakeRealtimeSession:
    def __init__(
        self,
        model_audio_24k: bytes,
        *,
        reconnect_delay_seconds: float = 0,
        restart_on_interruption: bool = True,
    ) -> None:
        self.model_audio_24k = model_audio_24k
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.restart_on_interruption = restart_on_interruption
        self.connected = False
        self.closed = False
        self.connect_calls = 0
        self.close_calls = 0
        self.appended_bytes = 0
        self.append_sizes: list[int] = []
        self.cancel_calls = 0
        self.interruption_calls: list[str | None] = []
        self.append_calls = 0
        self.speech_started_turns: list[int] = []
        self.turn_id_starts: list[int] = []
        self.instructions: list[str] = []
        self.second_turn_announced = False
        self.completed_first_turn = False
        self.on_speech_started: Callable[[int], Awaitable[None]] | None = None
        self.on_delta: Callable[[int, bytes], Awaitable[None]] | None = None
        self.on_turn_completed: Callable[[RealtimeTurnResult], Awaitable[None]] | None = None

    def bind(
        self,
        on_speech_started: Callable[[int], Awaitable[None]],
        on_delta: Callable[[int, bytes], Awaitable[None]],
        on_turn_completed: Callable[[RealtimeTurnResult], Awaitable[None]],
        turn_id_start: int,
        instructions: str,
    ):
        self.on_speech_started = on_speech_started
        self.on_delta = on_delta
        self.on_turn_completed = on_turn_completed
        self.turn_id_starts.append(turn_id_start)
        self.instructions.append(instructions)
        return self

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls > 1 and self.reconnect_delay_seconds:
            await asyncio.sleep(self.reconnect_delay_seconds)
        self.connected = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    async def append_audio(self, input_pcm_16k: bytes) -> None:
        self.appended_bytes += len(input_pcm_16k)
        self.append_sizes.append(len(input_pcm_16k))
        self.append_calls += 1
        if self.append_calls == 3 and not self.completed_first_turn:
            self.completed_first_turn = True
            asyncio.create_task(self._complete_turn(1))
        if self.append_calls == 4 and not self.second_turn_announced:
            self.second_turn_announced = True
            asyncio.create_task(self._announce_speech_started(2))

    async def cancel_response(self) -> None:
        self.cancel_calls += 1

    async def handle_playback_interruption(
        self,
        *,
        interrupted_output_text: str | None = None,
    ) -> None:
        self.interruption_calls.append(interrupted_output_text)
        await self.cancel_response()

    async def _announce_speech_started(self, turn_id: int) -> None:
        assert self.on_speech_started is not None
        self.speech_started_turns.append(turn_id)
        await self.on_speech_started(turn_id)

    async def _complete_turn(self, turn_id: int) -> None:
        assert self.on_delta is not None
        assert self.on_turn_completed is not None
        await self._announce_speech_started(turn_id)
        await self.on_delta(turn_id, self.model_audio_24k)
        await self.on_turn_completed(
            RealtimeTurnResult(
                turn_id=turn_id,
                input_audio_bytes=self.appended_bytes,
                output_audio_bytes=len(self.model_audio_24k),
                input_transcript="hello",
                output_transcript="hello from model",
                event_counts={
                    "input_audio_buffer.committed": 1,
                    "response.audio.delta": 1,
                    "response.done": 1,
                },
                first_audio_delta_ms=10,
                response_done_ms=20,
            )
        )


def _test_config(*, tail_silence_ms: int, send_interval_ms: int = 10) -> GatewayConfig:
    return GatewayConfig(
        freeswitch=FreeSwitchConfig(media_host="127.0.0.1", media_port=0),
        playback=PlaybackConfig(
            send_interval_ms=send_interval_ms,
            tail_silence_ms=tail_silence_ms,
        ),
        vad=VadConfig(
            speech_rms_threshold=300,
            start_speech_ms=20,
            end_silence_ms=40,
            min_speech_ms=20,
            max_utterance_ms=1000,
            pre_speech_ms=0,
            keep_silence_ms=0,
            barge_in_enabled=True,
        ),
    )


def _phone_frame(value: int) -> bytes:
    return samples_to_pcm_s16le([value] * 160)
