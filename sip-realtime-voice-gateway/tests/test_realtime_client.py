from __future__ import annotations

import asyncio
import base64
import json

from websockets.legacy.server import serve

from app.config import RealtimeConfig
from app.realtime_client import (
    RealtimeStreamingSession,
    build_realtime_url,
    run_realtime_probe,
    run_server_vad_probe,
    sanitize_event,
)


def test_build_realtime_url_adds_or_replaces_model():
    assert (
        build_realtime_url("wss://example.test/realtime", "model-a")
        == "wss://example.test/realtime?model=model-a"
    )
    assert (
        build_realtime_url("wss://example.test/realtime?model=old&x=1", "model-b")
        == "wss://example.test/realtime?model=model-b&x=1"
    )


def test_sanitize_event_masks_audio_payloads():
    event = {"type": "response.audio.delta", "delta": "abcdef", "audio": "xyz"}

    assert sanitize_event(event) == {
        "type": "response.audio.delta",
        "delta": "<base64:6 chars>",
        "audio": "<base64:3 chars>",
    }


def test_sanitize_event_keeps_transcript_delta_readable():
    event = {"type": "response.audio_transcript.delta", "delta": "hello"}

    assert sanitize_event(event)["delta"] == "hello"


def test_run_realtime_probe_with_fake_server():
    asyncio.run(_assert_realtime_probe_with_fake_server())


def test_realtime_streaming_session_uses_one_connection_for_multiple_turns():
    asyncio.run(_assert_streaming_session_multiple_turns())


def test_run_server_vad_probe_with_fake_server():
    asyncio.run(_assert_server_vad_probe_with_fake_server())


async def _assert_realtime_probe_with_fake_server() -> None:
    captured = {"headers": None, "path": None, "event_types": []}
    callback_audio: list[bytes] = []
    output_audio = b"\x01\x02\x03\x04"

    async def capture_audio(delta: bytes) -> None:
        callback_audio.append(delta)

    async def handler(websocket):
        captured["headers"] = websocket.request_headers
        captured["path"] = websocket.path
        async for raw_message in websocket:
            event = json.loads(raw_message)
            captured["event_types"].append(event["type"])
            if event["type"] == "response.create":
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio_transcript.delta",
                            "delta": "hello",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio.delta",
                            "delta": base64.b64encode(output_audio).decode("ascii"),
                        }
                    )
                )
                await websocket.send(json.dumps({"type": "response.done"}))
                break

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        config = RealtimeConfig(
            url=f"ws://127.0.0.1:{port}/realtime",
            model="test-realtime",
            voice="Cherry",
        )

        result, audio = await run_realtime_probe(
            config,
            api_key="test-key",
            input_pcm=b"\x00\x00" * 320,
            instructions="test",
            chunk_ms=20,
            send_delay_ms=0,
            timeout_seconds=5,
            on_audio_delta=capture_audio,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["path"] == "/realtime?model=test-realtime"
    assert captured["event_types"] == [
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ]
    assert audio == output_audio
    assert callback_audio == [output_audio]
    assert result.output_audio_bytes == len(output_audio)
    assert result.output_transcript == "hello"
    assert result.event_counts["response.audio.delta"] == 1


async def _assert_streaming_session_multiple_turns() -> None:
    captured = {"connections": 0, "event_types": []}
    output_audio = b"\x01\x02\x03\x04"
    callback_audio: list[tuple[int, bytes]] = []

    async def capture_audio(turn_id: int, delta: bytes) -> None:
        callback_audio.append((turn_id, delta))

    async def handler(websocket):
        captured["connections"] += 1
        async for raw_message in websocket:
            event = json.loads(raw_message)
            captured["event_types"].append(event["type"])
            if event["type"] == "response.create":
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio.delta",
                            "delta": base64.b64encode(output_audio).decode("ascii"),
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio_transcript.done",
                            "transcript": "ok",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"status": "completed"},
                        }
                    )
                )

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        config = RealtimeConfig(
            url=f"ws://127.0.0.1:{port}/realtime",
            model="test-realtime",
            voice="Cherry",
        )
        session = RealtimeStreamingSession(
            config,
            api_key="test-key",
            instructions="test",
            on_audio_delta=capture_audio,
        )
        await session.connect()
        try:
            await session.append_audio(b"\x00\x00" * 320)
            first = await session.commit_and_create_response(
                turn_id=1,
                input_audio_bytes=640,
            )
            result1 = await asyncio.wait_for(first, timeout=3)

            await session.append_audio(b"\x00\x00" * 320)
            second = await session.commit_and_create_response(
                turn_id=2,
                input_audio_bytes=640,
            )
            result2 = await asyncio.wait_for(second, timeout=3)
        finally:
            await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert captured["connections"] == 1
    assert captured["event_types"].count("session.update") == 1
    assert captured["event_types"].count("input_audio_buffer.append") == 2
    assert captured["event_types"].count("input_audio_buffer.commit") == 2
    assert captured["event_types"].count("response.create") == 2
    assert callback_audio == [(1, output_audio), (2, output_audio)]
    assert result1.output_transcript == "ok"
    assert result2.output_transcript == "ok"


async def _assert_server_vad_probe_with_fake_server() -> None:
    captured = {"event_types": [], "turn_detection": None}
    output_audio = b"\x05\x06\x07\x08"

    async def handler(websocket):
        append_count = 0
        async for raw_message in websocket:
            event = json.loads(raw_message)
            event_type = event["type"]
            captured["event_types"].append(event_type)
            if event_type == "session.update":
                captured["turn_detection"] = event["session"]["turn_detection"]
                continue
            if event_type == "input_audio_buffer.append":
                append_count += 1
                if append_count < 2:
                    continue
                await websocket.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.speech_started",
                            "item_id": "item_1",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.speech_stopped",
                            "item_id": "item_1",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.committed",
                            "item_id": "item_1",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.created",
                            "response": {"id": "resp_1"},
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": "item_1",
                            "transcript": "hello",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio.delta",
                            "response_id": "resp_1",
                            "delta": base64.b64encode(output_audio).decode("ascii"),
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio_transcript.done",
                            "response_id": "resp_1",
                            "transcript": "ok",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {"id": "resp_1", "status": "completed"},
                        }
                    )
                )
                break

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        config = RealtimeConfig(
            url=f"ws://127.0.0.1:{port}/realtime",
            model="test-realtime",
            voice="Cherry",
        )
        result, audio = await run_server_vad_probe(
            config,
            api_key="test-key",
            input_pcm=b"\x00\x00" * 320,
            instructions="test",
            chunk_ms=20,
            send_delay_ms=0,
            trailing_silence_ms=20,
            timeout_seconds=5,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert captured["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 800,
    }
    assert "input_audio_buffer.commit" not in captured["event_types"]
    assert "response.create" not in captured["event_types"]
    assert audio == output_audio
    assert result.input_transcript == "hello"
    assert result.output_transcript == "ok"
    assert result.response_ids == ["resp_1"]
    assert result.item_ids == ["item_1"]
    assert result.event_counts["input_audio_buffer.speech_started"] == 1
    assert result.event_counts["response.audio.delta"] == 1
