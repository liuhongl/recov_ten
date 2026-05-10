from __future__ import annotations

import asyncio
import json

from websockets.exceptions import ConnectionClosedError
from websockets.legacy.client import connect

from app.config import FreeSwitchConfig
from app.freeswitch_media import FreeSwitchMediaEchoServer


def test_freeswitch_media_echoes_binary_audio_frames():
    asyncio.run(_assert_binary_echo_roundtrip())


def test_freeswitch_media_replies_to_ping_control_message():
    asyncio.run(_assert_ping_roundtrip())


def test_freeswitch_media_rejects_unsupported_paths():
    asyncio.run(_assert_unsupported_path_is_rejected())


def test_freeswitch_media_supports_resample_echo_mode():
    asyncio.run(_assert_resample_echo_roundtrip())


def test_freeswitch_media_rejects_non_target_contract():
    try:
        FreeSwitchMediaEchoServer(FreeSwitchConfig(sample_rate=16000))
    except ValueError as err:
        assert "sample_rate=8000" in str(err)
    else:
        raise AssertionError("non-target media contract was not rejected")


async def _assert_binary_echo_roundtrip() -> None:
    server = FreeSwitchMediaEchoServer(
        FreeSwitchConfig(media_host="127.0.0.1", media_port=0),
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-call",
            ping_interval=None,
        ) as ws:
            payload = bytes(index % 256 for index in range(320))
            await ws.send(payload)
            actual = await asyncio.wait_for(ws.recv(), timeout=3)

        assert actual == payload
    finally:
        await server.stop()

    assert len(server.completed_sessions) == 1
    stats = server.completed_sessions[0]
    assert stats.call_id == "test-call"
    assert stats.inbound_frames == 1
    assert stats.outbound_frames == 1
    assert stats.inbound_bytes == 320
    assert stats.outbound_bytes == 320
    assert stats.invalid_frame_count == 0


async def _assert_resample_echo_roundtrip() -> None:
    server = FreeSwitchMediaEchoServer(
        FreeSwitchConfig(
            media_host="127.0.0.1",
            media_port=0,
            echo_mode="resample_16k_roundtrip",
        ),
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-resample",
            ping_interval=None,
        ) as ws:
            payload = bytes(index % 256 for index in range(320))
            await ws.send(payload)
            actual = await asyncio.wait_for(ws.recv(), timeout=3)

        assert len(actual) == len(payload)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.call_id == "test-resample"
    assert stats.inbound_bytes == 320
    assert stats.outbound_bytes == 320


async def _assert_ping_roundtrip() -> None:
    server = FreeSwitchMediaEchoServer(
        FreeSwitchConfig(media_host="127.0.0.1", media_port=0),
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-control",
            ping_interval=None,
        ) as ws:
            await ws.send(json.dumps({"type": "ping"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=3)

        payload = json.loads(raw)
        assert payload["type"] == "pong"
        assert payload["call_id"] == "test-control"
    finally:
        await server.stop()


async def _assert_unsupported_path_is_rejected() -> None:
    server = FreeSwitchMediaEchoServer(
        FreeSwitchConfig(media_host="127.0.0.1", media_port=0),
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/not-media/test-call",
            ping_interval=None,
        ) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
            except ConnectionClosedError as err:
                assert err.rcvd is not None
                assert err.rcvd.code == 1008
            else:
                raise AssertionError("unsupported path was not rejected")
    finally:
        await server.stop()
