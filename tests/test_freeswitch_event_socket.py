from __future__ import annotations

import asyncio
import json

from app.freeswitch_event_socket import (
    EventSocketMessage,
    FreeSwitchEventSocketClient,
    parse_channel_event,
    parse_playback_event,
)


def test_parse_chunk_played_playback_event():
    event = parse_playback_event(
        EventSocketMessage(
            headers={"Content-Type": "text/event-plain"},
            body=(
                "Event-Name: CUSTOM\n"
                "Event-Subclass: mod_audio_stream::playback\n"
                "Unique-ID: uuid-1\n"
                "\n"
                '{"event":"chunk_played","seq":12,"size":320,"remaining":5}'
            ),
        )
    )

    assert event is not None
    assert event.uuid == "uuid-1"
    assert event.event == "chunk_played"
    assert event.seq == 12
    assert event.size == 320
    assert event.remaining == 5
    assert event.is_queue_completed is False


def test_parse_queue_completed_playback_event():
    event = parse_playback_event(
        EventSocketMessage(
            headers={"Content-Type": "text/event-plain"},
            body=(
                "Event-Name: CUSTOM\r\n"
                "Event-Subclass: mod_audio_stream::playback\r\n"
                "Unique-ID: uuid-1\r\n"
                "\r\n"
                '{"event":"queue_completed","total_chunks":12}'
            ),
        )
    )

    assert event is not None
    assert event.event == "queue_completed"
    assert event.total_chunks == 12
    assert event.is_queue_completed is True


def test_parse_channel_hangup_complete_event():
    event = parse_channel_event(
        EventSocketMessage(
            headers={"Content-Type": "text/event-plain"},
            body=(
                "Event-Name: CHANNEL_HANGUP_COMPLETE\n"
                "Unique-ID: call-1\n"
                "variable_sip_realtime_gateway_call_id: call-1\n"
                "Hangup-Cause: NORMAL_CLEARING\n"
                "variable_sip_term_status: 200\n"
                "\n"
            ),
        )
    )

    assert event is not None
    assert event.name == "CHANNEL_HANGUP_COMPLETE"
    assert event.call_id == "call-1"
    assert event.unique_id == "call-1"
    assert event.hangup_cause == "NORMAL_CLEARING"
    assert event.sip_status == "200"


def test_event_socket_client_subscribes_reads_event_and_breaks():
    asyncio.run(_assert_event_socket_client_roundtrip())


def test_event_socket_client_subscribes_channel_events():
    asyncio.run(_assert_event_socket_client_channel_event())


async def _assert_event_socket_client_roundtrip() -> None:
    done = asyncio.Event()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(b"Content-Type: auth/request\n\n")
        await writer.drain()

        assert await _read_command(reader) == "auth test-pass"
        writer.write(
            b"Content-Type: command/reply\n"
            b"Reply-Text: +OK accepted\n\n"
        )
        await writer.drain()

        assert (
            await _read_command(reader)
            == "event plain CUSTOM mod_audio_stream::playback"
        )
        writer.write(
            b"Content-Type: command/reply\n"
            b"Reply-Text: +OK event listener enabled plain\n\n"
        )
        await writer.drain()

        event_body = (
            "Event-Name: CUSTOM\n"
            "Event-Subclass: mod_audio_stream::playback\n"
            "Unique-ID: uuid-1\n"
            "\n"
            + json.dumps(
                {
                    "event": "chunk_played",
                    "seq": 7,
                    "size": 320,
                    "remaining": 0,
                },
                separators=(",", ":"),
            )
        ).encode("utf-8")
        writer.write(
            b"Content-Type: text/event-plain\n"
            + f"Content-Length: {len(event_body)}\n\n".encode("utf-8")
            + event_body
        )
        await writer.drain()

        assert await _read_command(reader) == "api uuid_audio_stream uuid-1 break"
        writer.write(b"Content-Type: api/response\nContent-Length: 3\n\n+OK")
        await writer.drain()

        writer.close()
        await writer.wait_closed()
        done.set()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        client = FreeSwitchEventSocketClient(
            host="127.0.0.1",
            port=port,
            password="test-pass",
        )
        await client.connect()
        await client.subscribe_playback_events()

        event = await asyncio.wait_for(client.read_playback_event(), timeout=1)
        assert event.uuid == "uuid-1"
        assert event.seq == 7
        assert event.remaining == 0

        assert await client.break_audio_stream("uuid-1") is True
        await asyncio.wait_for(done.wait(), timeout=1)
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


async def _assert_event_socket_client_channel_event() -> None:
    done = asyncio.Event()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(b"Content-Type: auth/request\n\n")
        await writer.drain()

        assert await _read_command(reader) == "auth test-pass"
        writer.write(
            b"Content-Type: command/reply\n"
            b"Reply-Text: +OK accepted\n\n"
        )
        await writer.drain()

        assert (
            await _read_command(reader)
            == (
                "event plain CHANNEL_CREATE CHANNEL_PROGRESS "
                "CHANNEL_PROGRESS_MEDIA CHANNEL_ANSWER CHANNEL_HANGUP "
                "CHANNEL_HANGUP_COMPLETE"
            )
        )
        writer.write(
            b"Content-Type: command/reply\n"
            b"Reply-Text: +OK event listener enabled plain\n\n"
        )
        await writer.drain()

        event_body = (
            "Event-Name: CHANNEL_ANSWER\n"
            "Unique-ID: call-1\n"
            "variable_sip_realtime_gateway_call_id: call-1\n"
            "\n"
        ).encode("utf-8")
        writer.write(
            b"Content-Type: text/event-plain\n"
            + f"Content-Length: {len(event_body)}\n\n".encode("utf-8")
            + event_body
        )
        await writer.drain()

        writer.close()
        await writer.wait_closed()
        done.set()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        client = FreeSwitchEventSocketClient(
            host="127.0.0.1",
            port=port,
            password="test-pass",
        )
        await client.connect()
        await client.subscribe_channel_events()

        event = await asyncio.wait_for(client.read_channel_event(), timeout=1)
        assert event.call_id == "call-1"
        assert event.name == "CHANNEL_ANSWER"

        await asyncio.wait_for(done.wait(), timeout=1)
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


async def _read_command(reader: asyncio.StreamReader) -> str:
    data = bytearray()
    while True:
        chunk = await reader.read(1)
        if not chunk:
            raise EOFError("client closed")
        data.extend(chunk)
        if data.endswith(b"\n\n") or data.endswith(b"\r\n\r\n"):
            return data.decode("utf-8").strip()
