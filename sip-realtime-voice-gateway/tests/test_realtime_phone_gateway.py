from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from websockets.legacy.client import connect

from app.audio_codec import samples_to_pcm_s16le
from app.config import FreeSwitchConfig, GatewayConfig, VadConfig
from app.realtime_client import RealtimeTurnResult
from app.realtime_phone_gateway import FreeSwitchRealtimeGatewayServer


def test_realtime_phone_gateway_plays_model_audio_back_to_client():
    asyncio.run(_assert_realtime_phone_gateway_roundtrip())


def test_realtime_phone_gateway_clears_playback_on_user_interrupt():
    asyncio.run(_assert_realtime_phone_gateway_interrupts_playback())


async def _assert_realtime_phone_gateway_roundtrip() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 480))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(),
        api_key="test-key",
        realtime_session_factory=lambda on_delta: fake_session.bind(on_delta),
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
    finally:
        await server.stop()

    assert fake_session.connected is True
    assert fake_session.closed is True
    assert fake_session.appended_bytes == 1920
    assert fake_session.commits == [1]
    assert len(server.completed_sessions) == 1
    stats = server.completed_sessions[0]
    assert stats.turns_started == 1
    assert stats.turns_committed == 1
    assert stats.turns_completed == 1
    assert stats.turns_failed == 0
    assert stats.outbound_frames == 1
    assert stats.output_transcripts == ["hello from model"]


async def _assert_realtime_phone_gateway_interrupts_playback() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 480 * 20))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(),
        api_key="test-key",
        realtime_session_factory=lambda on_delta: fake_session.bind(on_delta),
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
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.interruptions == 1
    assert stats.dropped_playback_frames > 0
    assert stats.dropped_stale_frames == 0
    assert stats.ignored_busy_frames == 0
    assert fake_session.cancel_calls == 0


class FakeRealtimeSession:
    def __init__(self, model_audio_24k: bytes) -> None:
        self.model_audio_24k = model_audio_24k
        self.connected = False
        self.closed = False
        self.appended_bytes = 0
        self.clear_calls = 0
        self.cancel_calls = 0
        self.commits: list[int] = []
        self.on_delta: Callable[[int, bytes], Awaitable[None]] | None = None

    def bind(self, on_delta: Callable[[int, bytes], Awaitable[None]]):
        self.on_delta = on_delta
        return self

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def append_audio(self, input_pcm_16k: bytes) -> None:
        self.appended_bytes += len(input_pcm_16k)

    async def clear_input_buffer(self) -> None:
        self.clear_calls += 1

    async def commit_and_create_response(
        self,
        *,
        turn_id: int,
        input_audio_bytes: int,
    ) -> asyncio.Future[RealtimeTurnResult]:
        self.commits.append(turn_id)
        future: asyncio.Future[RealtimeTurnResult] = asyncio.get_running_loop().create_future()

        async def complete() -> None:
            assert self.on_delta is not None
            await self.on_delta(turn_id, self.model_audio_24k)
            future.set_result(
                RealtimeTurnResult(
                    turn_id=turn_id,
                    input_audio_bytes=input_audio_bytes,
                    output_audio_bytes=len(self.model_audio_24k),
                    input_transcript="hello",
                    output_transcript="hello from model",
                    event_counts={"response.audio.delta": 1, "response.done": 1},
                    first_audio_delta_ms=10,
                    response_done_ms=20,
                )
            )

        asyncio.create_task(complete())
        return future

    async def cancel_response(self) -> None:
        self.cancel_calls += 1


def _test_config() -> GatewayConfig:
    return GatewayConfig(
        freeswitch=FreeSwitchConfig(media_host="127.0.0.1", media_port=0),
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
