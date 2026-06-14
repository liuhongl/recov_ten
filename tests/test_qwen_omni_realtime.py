from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlparse

from websockets.legacy.server import serve

from app.qwen_omni_realtime import QwenOmniRealtimeServerVadSession
from app.qwen_omni_realtime_client import (
    QwenOmniRealtimeCredentials,
    QwenOmniRealtimeSessionConfig,
)
from app.realtime_types import RealtimeTurnResult


def test_qwen_omni_realtime_server_vad_session_maps_events_to_gateway_callbacks():
    asyncio.run(_assert_server_vad_session_maps_events_to_gateway_callbacks())


async def _assert_server_vad_session_maps_events_to_gateway_callbacks() -> None:
    expected_audio = b"\x11\x22"
    captured = {
        "headers": {},
        "query": {},
        "types": [],
        "audio": b"",
        "session_update": {},
    }
    speech_started_turns: list[int] = []
    input_transcripts: list[tuple[int, str]] = []
    audio_deltas: list[tuple[int, bytes]] = []
    turn_completed = asyncio.Event()
    completed_results: list[RealtimeTurnResult] = []

    async def handler(websocket):
        captured["headers"] = dict(websocket.request_headers)
        captured["query"] = parse_qs(urlparse(websocket.path).query)
        await websocket.send(
            json.dumps(
                {
                    "type": "session.created",
                    "session": {"id": "sess-a", "model": "qwen3-omni-flash-realtime"},
                }
            )
        )
        async for raw_message in websocket:
            event = json.loads(raw_message)
            captured["types"].append(event["type"])
            if event["type"] == "session.update":
                captured["session_update"] = event
                await websocket.send(
                    json.dumps({"type": "session.updated", "session": event["session"]})
                )
            elif event["type"] == "input_audio_buffer.append":
                captured["audio"] += base64.b64decode(event["audio"])
                await websocket.send(
                    json.dumps({"type": "input_audio_buffer.speech_started"})
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": "我要查费用。",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.created",
                            "response": {"id": "resp-a"},
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio.delta",
                            "delta": base64.b64encode(expected_audio).decode("ascii"),
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio_transcript.done",
                            "transcript": "好的，我帮您查一下。",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {
                                "id": "resp-a",
                                "usage": {
                                    "total_tokens": 130,
                                    "input_tokens": 70,
                                    "output_tokens": 60,
                                    "input_tokens_details": {
                                        "text_tokens": 10,
                                        "audio_tokens": 40,
                                    },
                                    "output_tokens_details": {
                                        "text_tokens": 5,
                                        "audio_tokens": 35,
                                    },
                                },
                            },
                        }
                    )
                )
                break

    async def on_speech_started(turn_id: int) -> None:
        speech_started_turns.append(turn_id)

    async def on_input_transcript(turn_id: int, text: str) -> None:
        input_transcripts.append((turn_id, text))

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        audio_deltas.append((turn_id, audio))

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        completed_results.append(result)
        turn_completed.set()

    server = await serve(handler, "127.0.0.1", 0)
    session: QwenOmniRealtimeServerVadSession | None = None
    try:
        port = server.sockets[0].getsockname()[1]
        session = QwenOmniRealtimeServerVadSession(
            QwenOmniRealtimeCredentials(
                api_key="sk-test",
                websocket_url=f"ws://127.0.0.1:{port}/api-ws/v1/realtime",
                model="qwen3-omni-flash-realtime",
            ),
            QwenOmniRealtimeSessionConfig(
                instructions="电话客服",
                voice="Cherry",
                manual_turn_detection=False,
            ),
            turn_id_start=7,
            on_speech_started=on_speech_started,
            on_input_transcript=on_input_transcript,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )

        await session.connect()
        await session.append_audio(b"\x00" * 640)
        await asyncio.wait_for(turn_completed.wait(), timeout=3)
    finally:
        if session is not None:
            await session.close()
        server.close()
        await server.wait_closed()

    assert (
        captured["headers"].get("Authorization")
        or captured["headers"].get("authorization")
    ) == "Bearer sk-test"
    assert captured["query"]["model"] == ["qwen3-omni-flash-realtime"]
    assert captured["types"] == ["session.update", "input_audio_buffer.append"]
    assert captured["audio"] == b"\x00" * 640
    assert captured["session_update"]["session"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "silence_duration_ms": 800,
    }
    assert speech_started_turns == [8]
    assert input_transcripts == [(8, "我要查费用。")]
    assert audio_deltas == [(8, expected_audio)]
    assert len(completed_results) == 1
    result = completed_results[0]
    assert result.turn_id == 8
    assert result.input_audio_bytes == 640
    assert result.output_audio_bytes == len(expected_audio)
    assert result.input_transcript == "我要查费用。"
    assert result.output_transcript == "好的，我帮您查一下。"
    assert result.first_audio_delta_ms is not None
    assert result.response_done_ms is not None
    assert result.status == "completed"
    assert result.response_id == "resp-a"
    assert result.event_counts["response.done"] == 1
