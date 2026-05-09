from __future__ import annotations

import asyncio
import base64
import json

from websockets.legacy.server import serve

from app.config import RealtimeConfig
from app.realtime_client import (
    build_realtime_url,
    run_realtime_probe,
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
    event = {"type": "response.audio_transcript.delta", "delta": "你好"}

    assert sanitize_event(event)["delta"] == "你好"


def test_run_realtime_probe_with_fake_server():
    asyncio.run(_assert_realtime_probe_with_fake_server())


async def _assert_realtime_probe_with_fake_server() -> None:
    captured = {"headers": None, "path": None, "event_types": []}
    output_audio = b"\x01\x02\x03\x04"

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
                            "delta": "你好",
                        },
                        ensure_ascii=False,
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
    assert result.output_audio_bytes == len(output_audio)
    assert result.output_transcript == "你好"
    assert result.event_counts["response.audio.delta"] == 1
