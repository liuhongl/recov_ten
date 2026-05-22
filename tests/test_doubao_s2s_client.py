from __future__ import annotations

import asyncio
import json
import struct

import pytest
from websockets.legacy.server import serve

from app.doubao_s2s_client import (
    COMPRESSION_NONE,
    DEFAULT_REALTIME_APP_KEY,
    DEFAULT_RESOURCE_ID,
    EVENT_CLIENT_INTERRUPT,
    EVENT_CHAT_RESPONSE,
    EVENT_CONNECTION_STARTED,
    EVENT_START_CONNECTION,
    EVENT_START_SESSION,
    EVENT_TASK_AUDIO,
    EVENT_TTS_AUDIO_DATA,
    EVENT_TTS_FINISHED,
    EVENT_USER_TEXT,
    MESSAGE_TYPE_AUDIO_ONLY_SERVER,
    MESSAGE_TYPE_FULL_SERVER,
    SERIALIZATION_JSON,
    DoubaoS2SCredentials,
    DoubaoS2SSessionConfig,
    build_audio_event_frame,
    build_event_frame,
    build_json_event_frame,
    build_start_session_payload,
    build_websocket_headers,
    decode_event,
    parse_frame,
    run_doubao_s2s_audio_probe,
    run_doubao_s2s_text_probe,
    _format_handshake_error,
    _redact_provider_error,
)
from app.realtime_types import RealtimeDialogConfig, RealtimeDialogContextItem


class _InvalidStatus:
    status_code = 401

    def __init__(self):
        self.headers = {
            "X-Tt-Logid": "log-a",
            "X-Api-Status-Code": "auth-failed",
            "X-Api-Message": "unauthorized",
        }


def test_websocket_headers_include_v2_credentials():
    credentials = _credentials()

    headers = build_websocket_headers(credentials, connect_id="conn-a")

    assert headers["X-Api-App-ID"] == "app-a"
    assert headers["X-Api-App-Key"] == DEFAULT_REALTIME_APP_KEY
    assert headers["X-Api-Access-Key"] == "token-a"
    assert headers["X-Api-Resource-Id"] == DEFAULT_RESOURCE_ID
    assert headers["X-Api-Connect-Id"] == "conn-a"
    assert headers["X-Api-Request-Id"] == "conn-a"
    assert "X-Tt-Logid" in headers


def test_start_session_payload_uses_selected_speaker():
    payload = build_start_session_payload(
        DoubaoS2SSessionConfig(speaker="zh_female_vv_jupiter_bigtts")
    )

    assert payload["tts"]["speaker"] == "zh_female_vv_jupiter_bigtts"
    assert payload["tts"]["audio_config"] == {
        "channel": 1,
        "format": "pcm",
        "sample_rate": 24000,
        "bits": 16,
    }
    assert payload["asr"]["language"] == "zh-CN"


def test_start_session_payload_includes_dialog_identity_fields():
    payload = build_start_session_payload(
        DoubaoS2SSessionConfig(
            system_prompt="完整业务提示词",
            dialog=RealtimeDialogConfig(
                bot_name="物业中心小明",
                system_role="你是物业中心小明，负责逾期费用提醒，禁止自称豆包。",
                speaking_style="电话客服口吻，简短、自然、礼貌但坚定。",
                model="1.2.1.1",
            ),
        )
    )

    assert payload["dialog"] == {
        "bot_name": "物业中心小明",
        "system_role": "你是物业中心小明，负责逾期费用提醒，禁止自称豆包。",
        "speaking_style": "电话客服口吻，简短、自然、礼貌但坚定。",
        "extra": {"model": "1.2.1.1"},
    }
    assert payload["prompt"]["system"] == "完整业务提示词"


def test_start_session_payload_includes_dialog_context():
    payload = build_start_session_payload(
        DoubaoS2SSessionConfig(
            dialog=RealtimeDialogConfig(
                bot_name="物业中心小明",
                model="1.2.1.1",
                dialog_context=(
                    RealtimeDialogContextItem(role="user", text="你是哪边？"),
                    RealtimeDialogContextItem(
                        role="assistant",
                        text="我是物业中心小明。",
                    ),
                ),
            ),
        )
    )

    assert payload["dialog"]["dialog_context"] == [
        {"role": "user", "text": "你是哪边？"},
        {"role": "assistant", "text": "我是物业中心小明。"},
    ]
    assert payload["dialog"]["extra"] == {"model": "1.2.1.1"}


def test_session_config_rejects_too_long_dialog_bot_name():
    config = DoubaoS2SSessionConfig(
        dialog=RealtimeDialogConfig(bot_name="一二三四五六七八九十一二三四五六七八九十一")
    )

    with pytest.raises(ValueError, match="dialog.bot_name"):
        config.validate()


def test_event_frame_roundtrip_with_session_id():
    raw = build_json_event_frame(
        EVENT_START_SESSION,
        {"hello": "world"},
        session_id="session-a",
    )

    frame = parse_frame(raw)

    assert frame.event == EVENT_START_SESSION
    assert frame.session_id == "session-a"
    assert frame.payload_json == {"hello": "world"}


def test_client_interrupt_frame_roundtrip_with_session_id():
    raw = build_json_event_frame(
        EVENT_CLIENT_INTERRUPT,
        {"session_id": "session-a"},
        session_id="session-a",
    )

    frame = parse_frame(raw)

    assert frame.event == EVENT_CLIENT_INTERRUPT
    assert frame.session_id == "session-a"
    assert frame.payload_json == {"session_id": "session-a"}


def test_connection_started_frame_roundtrip_with_connect_id():
    raw = _server_json_frame(
        EVENT_CONNECTION_STARTED,
        {"ok": True},
        connect_id="conn-server",
    )

    event = decode_event(parse_frame(raw))

    assert event.event == EVENT_CONNECTION_STARTED
    assert event.connect_id == "conn-server"
    assert event.payload == {"ok": True}


def test_audio_event_frame_roundtrip():
    raw = build_audio_event_frame(
        EVENT_TASK_AUDIO,
        b"\x01\x02\x03\x04",
        session_id="session-a",
    )

    frame = parse_frame(raw)

    assert frame.event == EVENT_TASK_AUDIO
    assert frame.session_id == "session-a"
    assert frame.payload == b"\x01\x02\x03\x04"


def test_format_handshake_error_keeps_provider_log_id():
    message = _format_handshake_error(_InvalidStatus())

    assert "HTTP 401" in message
    assert "auth-failed" in message
    assert "x_tt_logid=log-a" in message


def test_provider_error_redacts_invalid_app_key_value():
    message = _redact_provider_error(
        "invalid X-Api-App-Key: secret-value, expected:[fixed-value]"
    )

    assert "secret-value" not in message
    assert "expected:[fixed-value]" in message


def test_decode_event_extracts_asr_result_text():
    raw = _server_json_frame(
        451,
        {
            "results": [
                {
                    "text": "你好世界",
                    "is_interim": False,
                }
            ]
        },
        session_id="session-a",
    )

    event = decode_event(parse_frame(raw))

    assert event.text == "你好世界"
    assert event.is_final is True


def test_text_probe_with_fake_server():
    asyncio.run(_assert_text_probe_with_fake_server())


def test_audio_probe_with_fake_server():
    asyncio.run(_assert_audio_probe_with_fake_server())


async def _assert_text_probe_with_fake_server() -> None:
    captured = {"headers": None, "events": []}
    output_audio = _float32_audio(-0.5, 0.5)
    expected_pcm = b"\x00\xc0\x00\x40"

    async def handler(websocket):
        captured["headers"] = websocket.request_headers
        frame = parse_frame(await websocket.recv())
        captured["events"].append(frame.event)
        assert frame.event == EVENT_START_CONNECTION
        await websocket.send(
            _server_json_frame(
                EVENT_CONNECTION_STARTED,
                {"ok": True},
                connect_id="conn-server",
            )
        )

        frame = parse_frame(await websocket.recv())
        captured["events"].append(frame.event)
        assert frame.event == EVENT_START_SESSION
        await websocket.send(
            _server_json_frame(
                150,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            captured["events"].append(frame.event)
            if frame.event != EVENT_USER_TEXT:
                continue
            await websocket.send(
                _server_json_frame(
                    EVENT_CHAT_RESPONSE,
                    {"content": "你好，我是电话客服。"},
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_audio_frame(
                    EVENT_TTS_AUDIO_DATA,
                    output_audio,
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_FINISHED,
                    {"content": "done"},
                    session_id=frame.session_id,
                )
            )
            break

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result, audio = await run_doubao_s2s_text_probe(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            input_text="你好",
            timeout_seconds=3,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert captured["headers"]["X-Api-App-ID"] == "app-a"
    assert captured["events"] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_USER_TEXT,
    ]
    assert audio == expected_pcm
    assert result.output_audio_bytes == len(expected_pcm)
    assert result.output_transcript == "你好，我是电话客服。"
    assert result.event_counts[str(EVENT_TTS_AUDIO_DATA)] == 1


async def _assert_audio_probe_with_fake_server() -> None:
    captured = {"audio_bytes": 0}
    output_audio = _float32_audio(0.25)
    expected_pcm = b"\x00 "

    async def handler(websocket):
        frame = parse_frame(await websocket.recv())
        assert frame.event == EVENT_START_CONNECTION
        await websocket.send(
            _server_json_frame(
                EVENT_CONNECTION_STARTED,
                {"ok": True},
                connect_id="conn-server",
            )
        )
        frame = parse_frame(await websocket.recv())
        assert frame.event == EVENT_START_SESSION
        await websocket.send(
            _server_json_frame(150, {"ok": True}, session_id=frame.session_id)
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            if frame.event == EVENT_TASK_AUDIO:
                captured["audio_bytes"] += len(frame.payload)
                await websocket.send(
                    _server_audio_frame(
                        EVENT_TTS_AUDIO_DATA,
                        output_audio,
                        session_id=frame.session_id,
                    )
                )
                await websocket.send(
                    _server_json_frame(
                        EVENT_TTS_FINISHED,
                        {"content": "done"},
                        session_id=frame.session_id,
                    )
                )
                break

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result, audio = await run_doubao_s2s_audio_probe(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            input_pcm16_16k=b"\x00" * 640,
            trailing_silence_ms=0,
            timeout_seconds=3,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert captured["audio_bytes"] == 640
    assert audio == expected_pcm
    assert result.input_audio_bytes == 640
    assert result.output_audio_bytes == len(expected_pcm)


def _credentials(
    *,
    websocket_url: str = "wss://example.test/api/v3/realtime/dialogue",
) -> DoubaoS2SCredentials:
    return DoubaoS2SCredentials(
        app_id="app-a",
        access_token="token-a",
        websocket_url=websocket_url,
    )


def _server_json_frame(
    event: int,
    payload: dict,
    *,
    session_id: str = "",
    connect_id: str = "",
) -> bytes:
    return build_event_frame(
        message_type=MESSAGE_TYPE_FULL_SERVER,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        serialization=SERIALIZATION_JSON,
        compression=COMPRESSION_NONE,
        payload=json.dumps(payload).encode("utf-8"),
    )


def _server_audio_frame(event: int, payload: bytes, *, session_id: str) -> bytes:
    return build_event_frame(
        message_type=MESSAGE_TYPE_AUDIO_ONLY_SERVER,
        event=event,
        session_id=session_id,
        serialization=0,
        compression=COMPRESSION_NONE,
        payload=payload,
    )


def _float32_audio(*samples: float) -> bytes:
    return struct.pack(f"<{len(samples)}f", *samples)
