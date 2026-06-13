from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.legacy.server import serve

from app.qwen_omni_realtime_client import (
    DEFAULT_MODEL,
    DEFAULT_WS_URL,
    QwenOmniRealtimeCredentials,
    QwenOmniRealtimeSessionConfig,
    build_input_audio_append_event,
    build_session_update_event,
    build_websocket_headers,
    build_websocket_url,
    estimate_usage_cost_rmb,
    parse_response_usage,
    run_qwen_omni_realtime_audio_probe,
)


def test_websocket_url_adds_model_query():
    credentials = QwenOmniRealtimeCredentials(api_key="sk-test")

    url = build_websocket_url(credentials)

    parsed = urlparse(url)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "dashscope.aliyuncs.com"
    assert parse_qs(parsed.query)["model"] == [DEFAULT_MODEL]


def test_websocket_headers_use_bearer_token():
    headers = build_websocket_headers(QwenOmniRealtimeCredentials(api_key="sk-test"))

    assert headers == {"Authorization": "Bearer sk-test"}


def test_session_update_event_uses_manual_pcm_audio_session():
    event = build_session_update_event(
        QwenOmniRealtimeSessionConfig(
            voice="Cherry",
            instructions="你是电话客服。",
            manual_turn_detection=True,
            temperature=0.4,
            max_tokens=128,
        ),
        event_id="event-a",
    )

    assert event == {
        "event_id": "event-a",
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "voice": "Cherry",
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "instructions": "你是电话客服。",
            "turn_detection": None,
            "input_audio_transcription": {
                "model": "qwen3-asr-flash-realtime",
                "language": "zh",
            },
            "temperature": 0.4,
            "max_tokens": 128,
        },
    }


def test_audio_append_event_base64_encodes_pcm():
    event = build_input_audio_append_event(b"\x01\x02", event_id="event-a")

    assert event == {
        "event_id": "event-a",
        "type": "input_audio_buffer.append",
        "audio": "AQI=",
    }


def test_parse_usage_and_estimate_audio_output_cost():
    usage = parse_response_usage(
        {
            "type": "response.done",
            "response": {
                "usage": {
                    "total_tokens": 650,
                    "input_tokens": 300,
                    "output_tokens": 350,
                    "input_tokens_details": {
                        "text_tokens": 100,
                        "audio_tokens": 200,
                    },
                    "output_tokens_details": {
                        "text_tokens": 50,
                        "audio_tokens": 300,
                    },
                }
            },
        }
    )

    estimate = estimate_usage_cost_rmb(
        usage,
        model="qwen3-omni-flash-realtime",
        output_audio_enabled=True,
    )

    assert usage.input_text_tokens == 100
    assert usage.input_audio_tokens == 200
    assert usage.output_text_tokens == 50
    assert usage.output_audio_tokens == 300
    assert estimate.total_rmb == pytest.approx(
        (100 * 2.2 + 200 * 18.9 + 300 * 75.1) / 1_000_000
    )
    assert estimate.line_items["output_text"].billed is False
    assert estimate.line_items["output_text"].rmb == 0


def test_audio_probe_collects_output_transcripts_usage_and_cost():
    asyncio.run(_assert_audio_probe_collects_output_transcripts_usage_and_cost())


async def _assert_audio_probe_collects_output_transcripts_usage_and_cost():
    expected_audio = b"\x11\x22\x33\x44"
    captured = {
        "headers": {},
        "query": {},
        "types": [],
        "audio": b"",
        "session_update": {},
    }

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
            elif event["type"] == "input_audio_buffer.commit":
                await websocket.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.committed",
                            "item_id": "item-user",
                        }
                    )
                )
            elif event["type"] == "response.create":
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": "你好。",
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
                            "transcript": "您好，我是客服。",
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

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result, audio = await run_qwen_omni_realtime_audio_probe(
            QwenOmniRealtimeCredentials(
                api_key="sk-test",
                websocket_url=f"ws://127.0.0.1:{port}/api-ws/v1/realtime",
                model="qwen3-omni-flash-realtime",
            ),
            QwenOmniRealtimeSessionConfig(instructions="电话客服", voice="Cherry"),
            input_pcm16_16k=b"\x00" * 640,
            chunk_ms=20,
            send_delay_ms=0,
            timeout_seconds=3,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert (
        captured["headers"].get("Authorization")
        or captured["headers"].get("authorization")
    ) == "Bearer sk-test"
    assert captured["query"]["model"] == ["qwen3-omni-flash-realtime"]
    assert captured["types"] == [
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ]
    assert captured["audio"] == b"\x00" * 640
    assert captured["session_update"]["session"]["turn_detection"] is None
    assert audio == expected_audio
    assert result.session_id == "sess-a"
    assert result.input_audio_bytes == 640
    assert result.output_audio_bytes == len(expected_audio)
    assert result.input_transcript == "你好。"
    assert result.output_transcript == "您好，我是客服。"
    assert result.usage.input_audio_tokens == 40
    assert result.cost_estimate_rmb is not None
    assert result.event_counts["response.done"] == 1
