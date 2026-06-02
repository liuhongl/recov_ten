from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct

from websockets.legacy.server import serve

from app.audio_codec import float32le_to_pcm_s16le
from app.doubao_s2s_client import (
    COMPRESSION_NONE,
    EVENT_ASR_ENDED,
    EVENT_ASR_INFO,
    EVENT_ASR_RESPONSE,
    EVENT_CLIENT_INTERRUPT,
    EVENT_CHAT_ENDED,
    EVENT_CHAT_RESPONSE,
    EVENT_CONNECTION_STARTED,
    EVENT_FINISH_SESSION,
    EVENT_SESSION_FINISHED,
    EVENT_SESSION_STARTED,
    EVENT_START_CONNECTION,
    EVENT_START_SESSION,
    EVENT_SAY_HELLO,
    EVENT_TASK_AUDIO,
    EVENT_TTS_AUDIO_DATA,
    EVENT_TTS_FINISHED,
    EVENT_TTS_STARTED,
    MESSAGE_TYPE_AUDIO_ONLY_SERVER,
    MESSAGE_TYPE_FULL_SERVER,
    SERIALIZATION_JSON,
    DoubaoS2SCredentials,
    DoubaoS2SSessionConfig,
    build_event_frame,
    parse_frame,
)
from app.doubao_s2s_realtime import DoubaoS2SServerVadSession
from app.realtime_types import RealtimeTurnResult


def test_doubao_s2s_server_vad_session_streams_audio_turn():
    asyncio.run(_assert_server_vad_session_streams_audio_turn())


def test_doubao_s2s_notifies_final_input_transcript_before_audio():
    asyncio.run(_assert_doubao_s2s_notifies_input_transcript_before_audio())


def test_doubao_s2s_server_vad_session_sends_client_interrupt_without_restart():
    asyncio.run(_assert_interruption_sends_client_interrupt_without_restart())


def test_doubao_s2s_server_vad_session_prefers_gateway_restart_on_interruption():
    assert DoubaoS2SServerVadSession.restart_on_interruption is True


def test_doubao_s2s_seed_assistant_context_suppresses_audio_callbacks():
    asyncio.run(_assert_seed_assistant_context_suppresses_audio_callbacks())


def test_doubao_s2s_cancelled_context_seed_suppresses_late_audio_callbacks():
    asyncio.run(_assert_cancelled_context_seed_suppresses_late_audio_callbacks())


def test_doubao_s2s_missing_asr_end_does_not_leave_stale_response_turn():
    asyncio.run(_assert_missing_asr_end_does_not_leave_stale_response_turn())


def test_doubao_s2s_close_from_turn_completed_stops_reader_cleanly(caplog):
    asyncio.run(_assert_close_from_turn_completed_stops_reader_cleanly(caplog))


async def _assert_server_vad_session_streams_audio_turn() -> None:
    captured = {"events": [], "headers": None}
    output_audio = _float32_audio(0.25, -0.25)
    tail_audio = _float32_audio(0.5)
    expected_pcm = float32le_to_pcm_s16le(output_audio + tail_audio)
    completed = asyncio.Event()
    speech_started_turns: list[int] = []
    audio_deltas: list[tuple[int, bytes]] = []
    turn_results: list[RealtimeTurnResult] = []

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
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            captured["events"].append(frame.event)
            if frame.event != EVENT_TASK_AUDIO:
                continue
            await _send_basic_response_start(
                websocket,
                frame.session_id,
                input_text="hello",
                output_text="assistant hello",
                audio=output_audio,
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_CHAT_ENDED,
                    {"content": "done"},
                    session_id=frame.session_id,
                )
            )
            await asyncio.sleep(0.05)
            assert not completed.is_set()
            await websocket.send(
                _server_audio_frame(
                    EVENT_TTS_AUDIO_DATA,
                    tail_audio,
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_FINISHED,
                    {"content": "audio done"},
                    session_id=frame.session_id,
                )
            )
            break

    async def on_speech_started(turn_id: int) -> None:
        speech_started_turns.append(turn_id)

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        audio_deltas.append((turn_id, audio))

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        turn_results.append(result)
        completed.set()

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()
        await session.append_audio(b"\x00\x01" * 320)
        await asyncio.wait_for(completed.wait(), timeout=3)
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert captured["headers"]["X-Api-App-ID"] == "app-a"
    assert captured["events"] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_TASK_AUDIO,
    ]
    assert speech_started_turns == [1]
    assert [turn_id for turn_id, _ in audio_deltas] == [1, 1]
    assert b"".join(audio for _, audio in audio_deltas) == expected_pcm
    assert len(turn_results) == 1
    assert turn_results[0].turn_id == 1
    assert turn_results[0].input_transcript == "hello"
    assert turn_results[0].output_transcript == "assistant hello"
    assert turn_results[0].status == "completed"
    assert turn_results[0].event_counts[str(EVENT_ASR_RESPONSE)] == 1
    assert turn_results[0].event_counts[str(EVENT_CHAT_ENDED)] == 1
    assert turn_results[0].event_counts[str(EVENT_TTS_AUDIO_DATA)] == 2
    assert turn_results[0].event_counts[str(EVENT_TTS_FINISHED)] == 1


async def _assert_doubao_s2s_notifies_input_transcript_before_audio() -> None:
    output_audio = _float32_audio(0.25, -0.25)
    callback_order: list[str] = []
    completed = asyncio.Event()

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
            _server_json_frame(
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            if frame.event != EVENT_TASK_AUDIO:
                continue
            await _send_complete_response(
                websocket,
                frame.session_id,
                input_text="我要转人工",
                output_text="我帮您转接，请稍等。",
                audio=output_audio,
            )
            break

    async def on_speech_started(turn_id: int) -> None:
        del turn_id

    async def on_input_transcript(turn_id: int, text: str) -> None:
        callback_order.append(f"input:{turn_id}:{text}")

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        del audio
        callback_order.append(f"audio:{turn_id}")

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        callback_order.append(f"done:{result.turn_id}")
        completed.set()

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_input_transcript=on_input_transcript,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()
        await session.append_audio(b"\x00\x01" * 320)
        await asyncio.wait_for(completed.wait(), timeout=3)
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert callback_order == [
        "input:1:我要转人工",
        "audio:1",
        "done:1",
    ]


async def _assert_interruption_sends_client_interrupt_without_restart() -> None:
    captured = {"events": [], "session_ids": []}
    initial_audio = _float32_audio(0.25)
    first_audio = asyncio.Event()
    interrupted = asyncio.Event()
    cancelled = asyncio.Event()
    audio_deltas: list[tuple[int, bytes]] = []
    turn_results: list[RealtimeTurnResult] = []

    async def handler(websocket):
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
        captured["session_ids"].append(frame.session_id)
        assert frame.event == EVENT_START_SESSION
        await websocket.send(
            _server_json_frame(
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        task_audio_count = 0
        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            captured["events"].append(frame.event)

            if frame.event == EVENT_TASK_AUDIO:
                task_audio_count += 1
                assert task_audio_count == 1
                await _send_basic_response_start(
                    websocket,
                    frame.session_id,
                    input_text="old input",
                    output_text="old output",
                    audio=initial_audio,
                )
                continue

            if frame.event == EVENT_CLIENT_INTERRUPT:
                await websocket.send(
                    _server_json_frame(
                        EVENT_TTS_FINISHED,
                        {"content": "interrupted"},
                        session_id=frame.session_id,
                    )
                )
                interrupted.set()
                break

    async def on_speech_started(turn_id: int) -> None:
        return None

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        audio_deltas.append((turn_id, audio))
        if len(audio_deltas) == 1:
            first_audio.set()

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        turn_results.append(result)
        if result.status == "cancelled":
            cancelled.set()

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()

        await session.append_audio(b"\x00\x01" * 320)
        await asyncio.wait_for(first_audio.wait(), timeout=3)
        await session.handle_playback_interruption(
            interrupted_output_text="old output"
        )
        await asyncio.wait_for(interrupted.wait(), timeout=3)
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert captured["events"] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_TASK_AUDIO,
        EVENT_CLIENT_INTERRUPT,
    ]
    assert EVENT_FINISH_SESSION not in captured["events"]
    assert captured["events"].count(EVENT_START_SESSION) == 1
    assert len(captured["session_ids"]) == 1
    assert audio_deltas == [
        (1, float32le_to_pcm_s16le(initial_audio)),
    ]
    assert [result.status for result in turn_results] == ["cancelled"]
    assert turn_results[0].turn_id == 1
    assert turn_results[0].output_transcript == "old output"


async def _assert_missing_asr_end_does_not_leave_stale_response_turn() -> None:
    output_audio = _float32_audio(0.25)
    completed = asyncio.Event()
    turn_results: list[RealtimeTurnResult] = []

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
            _server_json_frame(
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            if frame.event != EVENT_TASK_AUDIO:
                continue
            await websocket.send(
                _server_json_frame(
                    EVENT_ASR_INFO,
                    {"status": "started"},
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_ASR_RESPONSE,
                    {"results": [{"text": "hello", "is_interim": False}]},
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_STARTED,
                    {},
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_CHAT_RESPONSE,
                    {"content": "assistant hello"},
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
                    {"content": "audio done"},
                    session_id=frame.session_id,
                )
            )
            await asyncio.sleep(0.05)
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_STARTED,
                    {"content": "late provider event"},
                    session_id=frame.session_id,
                )
            )
            async for _ in websocket:
                pass

    async def on_speech_started(turn_id: int) -> None:
        return None

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        return None

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        turn_results.append(result)
        completed.set()

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()
        await session.append_audio(b"\x00\x01" * 320)
        await asyncio.wait_for(completed.wait(), timeout=3)
        await asyncio.sleep(0.1)
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert [result.turn_id for result in turn_results] == [1]
    assert turn_results[0].input_transcript == "hello"
    assert turn_results[0].output_transcript == "assistant hello"


async def _assert_close_from_turn_completed_stops_reader_cleanly(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.doubao_s2s_realtime")
    output_audio = _float32_audio(0.25)
    completed = asyncio.Event()
    session_ref: dict[str, DoubaoS2SServerVadSession] = {}

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
            _server_json_frame(
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            if frame.event != EVENT_TASK_AUDIO:
                continue
            await _send_complete_response(
                websocket,
                frame.session_id,
                input_text="handoff",
                output_text="transfer",
                audio=output_audio,
            )
            async for _ in websocket:
                pass

    async def on_speech_started(turn_id: int) -> None:
        return None

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        return None

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        await session_ref["session"].close()
        completed.set()

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        session_ref["session"] = session
        await session.connect()
        await session.append_audio(b"\x00\x01" * 320)
        await asyncio.wait_for(completed.wait(), timeout=3)
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()

    assert [
        record
        for record in caplog.records
        if record.message == "doubao_s2s_realtime_reader_failed"
    ] == []


async def _assert_seed_assistant_context_suppresses_audio_callbacks() -> None:
    captured = {"events": []}
    seed_audio = _float32_audio(0.25, -0.25)
    audio_deltas: list[tuple[int, bytes]] = []
    turn_results: list[RealtimeTurnResult] = []
    speech_started_turns: list[int] = []

    async def handler(websocket):
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
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            captured["events"].append(frame.event)
            if frame.event != EVENT_SAY_HELLO:
                continue
            assert frame.payload_json == {
                "content": "seed opening",
                "session_id": frame.session_id,
            }
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_STARTED,
                    {},
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_audio_frame(
                    EVENT_TTS_AUDIO_DATA,
                    seed_audio,
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_FINISHED,
                    {"content": "seed done"},
                    session_id=frame.session_id,
                )
            )
            break

    async def on_speech_started(turn_id: int) -> None:
        speech_started_turns.append(turn_id)

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        audio_deltas.append((turn_id, audio))

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        turn_results.append(result)

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()
        await session.seed_assistant_context("seed opening")
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert captured["events"] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_SAY_HELLO,
    ]
    assert speech_started_turns == []
    assert audio_deltas == []
    assert turn_results == []


async def _assert_cancelled_context_seed_suppresses_late_audio_callbacks() -> None:
    captured = {"events": []}
    seed_audio = _float32_audio(0.25, -0.25)
    say_hello_seen = asyncio.Event()
    send_late_seed_events = asyncio.Event()
    audio_deltas: list[tuple[int, bytes]] = []
    turn_results: list[RealtimeTurnResult] = []
    speech_started_turns: list[int] = []

    async def handler(websocket):
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
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            captured["events"].append(frame.event)
            if frame.event != EVENT_SAY_HELLO:
                continue
            say_hello_seen.set()
            await send_late_seed_events.wait()
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_STARTED,
                    {},
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_audio_frame(
                    EVENT_TTS_AUDIO_DATA,
                    seed_audio,
                    session_id=frame.session_id,
                )
            )
            await websocket.send(
                _server_json_frame(
                    EVENT_TTS_FINISHED,
                    {"content": "late seed done"},
                    session_id=frame.session_id,
                )
            )
            async for _ in websocket:
                pass

    async def on_speech_started(turn_id: int) -> None:
        speech_started_turns.append(turn_id)

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        audio_deltas.append((turn_id, audio))

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        turn_results.append(result)

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()
        seed_task = asyncio.create_task(
            session.seed_assistant_context("seed opening")
        )
        await asyncio.wait_for(say_hello_seen.wait(), timeout=3)
        seed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await seed_task
        send_late_seed_events.set()
        await asyncio.sleep(0.1)
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert captured["events"] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_SAY_HELLO,
    ]
    assert speech_started_turns == []
    assert audio_deltas == []
    assert turn_results == []


def _credentials(
    *,
    websocket_url: str,
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


async def _send_basic_response_start(
    websocket,
    session_id: str,
    *,
    input_text: str,
    output_text: str,
    audio: bytes,
) -> None:
    await websocket.send(
        _server_json_frame(
            EVENT_ASR_INFO,
            {"status": "started"},
            session_id=session_id,
        )
    )
    await websocket.send(
        _server_json_frame(
            EVENT_ASR_RESPONSE,
            {"results": [{"text": input_text, "is_interim": False}]},
            session_id=session_id,
        )
    )
    await websocket.send(
        _server_json_frame(
            EVENT_ASR_ENDED,
            {},
            session_id=session_id,
        )
    )
    await websocket.send(
        _server_json_frame(
            EVENT_TTS_STARTED,
            {},
            session_id=session_id,
        )
    )
    await websocket.send(
        _server_json_frame(
            EVENT_CHAT_RESPONSE,
            {"content": output_text},
            session_id=session_id,
        )
    )
    await websocket.send(
        _server_audio_frame(
            EVENT_TTS_AUDIO_DATA,
            audio,
            session_id=session_id,
        )
    )


async def _send_complete_response(
    websocket,
    session_id: str,
    *,
    input_text: str,
    output_text: str,
    audio: bytes,
) -> None:
    await _send_basic_response_start(
        websocket,
        session_id,
        input_text=input_text,
        output_text=output_text,
        audio=audio,
    )
    await websocket.send(
        _server_json_frame(
            EVENT_TTS_FINISHED,
            {"content": "audio done"},
            session_id=session_id,
        )
    )


def _float32_audio(*samples: float) -> bytes:
    return struct.pack(f"<{len(samples)}f", *samples)
