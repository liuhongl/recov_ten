from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from .config import EventSocketConfig

LOGGER = logging.getLogger(__name__)

PLAYBACK_EVENT_SUBCLASS = "mod_audio_stream::playback"
CHANNEL_EVENT_NAMES = (
    "CHANNEL_CREATE",
    "CHANNEL_PROGRESS",
    "CHANNEL_PROGRESS_MEDIA",
    "CHANNEL_ANSWER",
    "CHANNEL_HANGUP",
    "CHANNEL_HANGUP_COMPLETE",
)
DEFAULT_HEADER_LIMIT_BYTES = 65536


class EventSocketError(RuntimeError):
    """Raised when FreeSWITCH Event Socket returns an unexpected response."""


@dataclass(frozen=True)
class EventSocketMessage:
    headers: dict[str, str]
    body: str = ""


@dataclass(frozen=True)
class PlaybackProgressEvent:
    uuid: str
    event: str
    seq: int | None = None
    size: int | None = None
    remaining: int | None = None
    total_chunks: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_queue_completed(self) -> bool:
        return self.event == "queue_completed" or self.remaining == 0


@dataclass(frozen=True)
class ChannelStateEvent:
    name: str
    call_id: str
    unique_id: str | None = None
    hangup_cause: str | None = None
    sip_status: str | None = None
    sip_reason: str | None = None
    raw: dict[str, str] | None = None


PlaybackEventHandler = Callable[
    [PlaybackProgressEvent],
    Awaitable[None] | None,
]


class FreeSwitchEventSocketClient:
    """Small inbound Event Socket client for playback control and events."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 18021,
        password: str = "ClueCon",
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.is_connected:
            return

        self._reader, self._writer = await asyncio.open_connection(
            self.host,
            self.port,
        )
        greeting = await self.read_message()
        if _get_header(greeting.headers, "Content-Type") != "auth/request":
            raise EventSocketError(
                "expected auth/request from FreeSWITCH Event Socket"
            )

        await self._send_command(f"auth {self.password}")
        reply = await self.read_message()
        if not _command_ok(reply):
            raise EventSocketError(
                f"FreeSWITCH Event Socket auth failed: {_reply_text(reply)}"
            )

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)

    async def subscribe_playback_events(self) -> None:
        await self._send_command(f"event plain CUSTOM {PLAYBACK_EVENT_SUBCLASS}")
        reply = await self.read_message()
        if not _command_ok(reply):
            raise EventSocketError(
                f"could not subscribe FreeSWITCH playback events: {_reply_text(reply)}"
            )

    async def subscribe_channel_events(self) -> None:
        await self._send_command("event plain " + " ".join(CHANNEL_EVENT_NAMES))
        reply = await self.read_message()
        if not _command_ok(reply):
            raise EventSocketError(
                f"could not subscribe FreeSWITCH channel events: {_reply_text(reply)}"
            )

    async def api(self, command: str) -> str:
        await self._send_command(f"api {command}")
        while True:
            reply = await self.read_message()
            content_type = _get_header(reply.headers, "Content-Type")
            if content_type in {"api/response", "command/reply"}:
                return _reply_text(reply)

    async def break_audio_stream(self, uuid: str) -> bool:
        uuid = uuid.strip()
        if not uuid:
            raise ValueError("uuid is required")
        reply = await self.api(f"uuid_audio_stream {uuid} break")
        return not reply.strip().startswith("-ERR")

    async def read_playback_event(self) -> PlaybackProgressEvent:
        while True:
            message = await self.read_message()
            event = parse_playback_event(message)
            if event is not None:
                return event

    async def read_channel_event(self) -> ChannelStateEvent:
        while True:
            message = await self.read_message()
            event = parse_channel_event(message)
            if event is not None:
                return event

    async def read_message(self) -> EventSocketMessage:
        if self._reader is None:
            raise EventSocketError("FreeSWITCH Event Socket is not connected")
        return await read_event_socket_message(self._reader)

    async def _send_command(self, command: str) -> None:
        if self._writer is None:
            raise EventSocketError("FreeSWITCH Event Socket is not connected")
        self._writer.write(f"{command}\n\n".encode("utf-8"))
        await self._writer.drain()


class FreeSwitchPlaybackController:
    """Persistent playback controller using FreeSWITCH Event Socket."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        password: str,
        on_playback_event: PlaybackEventHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.on_playback_event = on_playback_event
        self._api_client: FreeSwitchEventSocketClient | None = None
        self._event_client: FreeSwitchEventSocketClient | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._api_lock = asyncio.Lock()

    @classmethod
    def from_config(
        cls,
        config: EventSocketConfig,
        *,
        password: str,
        on_playback_event: PlaybackEventHandler | None = None,
    ) -> FreeSwitchPlaybackController:
        return cls(
            host=config.host,
            port=config.port,
            password=password,
            on_playback_event=on_playback_event,
        )

    async def start(self) -> None:
        self._api_client = await self._connect_client()
        if self.on_playback_event is None:
            return

        self._event_client = await self._connect_client()
        await self._event_client.subscribe_playback_events()
        self._event_task = asyncio.create_task(
            self._run_event_loop(),
            name="freeswitch-playback-events",
        )

    async def stop(self) -> None:
        if self._event_task is not None:
            self._event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None

        if self._event_client is not None:
            await self._event_client.close()
            self._event_client = None

        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None

    async def break_playback(self, media_uuid: str) -> bool:
        async with self._api_lock:
            client = await self._ensure_api_client()
            try:
                return await client.break_audio_stream(media_uuid)
            except (OSError, EOFError, EventSocketError):
                LOGGER.warning(
                    "freeswitch_event_socket_reconnect_before_break uuid=%s",
                    media_uuid,
                    exc_info=True,
                )
                await client.close()
                self._api_client = await self._connect_client()
                return await self._api_client.break_audio_stream(media_uuid)

    async def _run_event_loop(self) -> None:
        assert self.on_playback_event is not None

        while True:
            try:
                client = await self._ensure_event_client()
                event = await client.read_playback_event()
                result = self.on_playback_event(event)
                if result is not None:
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning(
                    "freeswitch_playback_event_loop_error",
                    exc_info=True,
                )
                await self._reset_event_client()
                await asyncio.sleep(0.2)

    async def _ensure_api_client(self) -> FreeSwitchEventSocketClient:
        if self._api_client is None or not self._api_client.is_connected:
            self._api_client = await self._connect_client()
        return self._api_client

    async def _ensure_event_client(self) -> FreeSwitchEventSocketClient:
        if self._event_client is None or not self._event_client.is_connected:
            self._event_client = await self._connect_client()
            await self._event_client.subscribe_playback_events()
        return self._event_client

    async def _reset_event_client(self) -> None:
        if self._event_client is None:
            return
        await self._event_client.close()
        self._event_client = None

    async def _connect_client(self) -> FreeSwitchEventSocketClient:
        client = FreeSwitchEventSocketClient(
            host=self.host,
            port=self.port,
            password=self.password,
        )
        await client.connect()
        return client


async def read_event_socket_message(
    reader: asyncio.StreamReader,
) -> EventSocketMessage:
    header_bytes = await _read_header_block(reader)
    header_text = header_bytes.decode("utf-8", errors="replace")
    headers = _parse_headers(header_text)
    content_length = _content_length(headers)
    if content_length <= 0:
        return EventSocketMessage(headers=headers)

    body_bytes = await reader.readexactly(content_length)
    body = body_bytes.decode("utf-8", errors="replace")
    return EventSocketMessage(headers=headers, body=body)


def parse_playback_event(
    message: EventSocketMessage,
) -> PlaybackProgressEvent | None:
    outer_content_type = _get_header(message.headers, "Content-Type")
    if outer_content_type == "text/event-plain":
        event_headers, event_body = parse_plain_event_body(message.body)
    else:
        event_headers, event_body = message.headers, message.body

    if _get_header(event_headers, "Event-Subclass") != PLAYBACK_EVENT_SUBCLASS:
        return None

    uuid = _first_header(
        event_headers,
        "Unique-ID",
        "Channel-Unique-ID",
        "Channel-Call-UUID",
        "variable_uuid",
        "Call-UUID",
        "Audio-Stream-UUID",
        "uuid",
    )
    if not uuid:
        return None

    payload = _parse_event_json(event_headers, event_body)
    if payload is None:
        return None

    event_type = str(payload.get("event", "")).strip()
    if not event_type:
        return None

    return PlaybackProgressEvent(
        uuid=uuid,
        event=event_type,
        seq=_optional_int(payload.get("seq")),
        size=_optional_int(payload.get("size")),
        remaining=_optional_int(payload.get("remaining")),
        total_chunks=_optional_int(payload.get("total_chunks")),
        raw=payload,
    )


def parse_channel_event(
    message: EventSocketMessage,
) -> ChannelStateEvent | None:
    outer_content_type = _get_header(message.headers, "Content-Type")
    if outer_content_type == "text/event-plain":
        event_headers, _event_body = parse_plain_event_body(message.body)
    else:
        event_headers = message.headers

    event_name = _get_header(event_headers, "Event-Name")
    if event_name not in CHANNEL_EVENT_NAMES:
        return None

    unique_id = _first_header(
        event_headers,
        "Unique-ID",
        "Channel-Unique-ID",
        "Channel-Call-UUID",
        "Call-UUID",
    )
    call_id = _first_header(
        event_headers,
        "variable_sip_realtime_gateway_call_id",
        "variable_origination_uuid",
        "variable_uuid",
        "Unique-ID",
        "Channel-Unique-ID",
        "Channel-Call-UUID",
        "Call-UUID",
    )
    if not call_id:
        return None

    return ChannelStateEvent(
        name=event_name,
        call_id=call_id,
        unique_id=unique_id,
        hangup_cause=_first_header(
            event_headers,
            "Hangup-Cause",
            "variable_hangup_cause",
            "variable_originate_disposition",
            "variable_proto_specific_hangup_cause",
        ),
        sip_status=_first_header(
            event_headers,
            "variable_sip_term_status",
            "variable_sip_invite_failure_status",
            "variable_sip_response_code",
            "variable_sip_hangup_disposition",
        ),
        sip_reason=_first_header(
            event_headers,
            "variable_sip_term_cause",
            "variable_sip_invite_failure_phrase",
            "variable_sip_hangup_phrase",
            "variable_proto_specific_hangup_cause",
        ),
        raw=event_headers,
    )


def parse_plain_event_body(body: str) -> tuple[dict[str, str], str]:
    normalized = body.replace("\r\n", "\n")
    header_text, separator, payload = normalized.partition("\n\n")
    if not separator:
        return _parse_headers(normalized), ""
    return _parse_headers(header_text), payload


async def _read_header_block(reader: asyncio.StreamReader) -> bytes:
    data = bytearray()
    while True:
        chunk = await reader.read(1)
        if not chunk:
            if data:
                raise EOFError("unexpected EOF while reading Event Socket headers")
            raise EOFError("Event Socket connection closed")
        data.extend(chunk)
        if data.endswith(b"\n\n"):
            return bytes(data[:-2])
        if data.endswith(b"\r\n\r\n"):
            return bytes(data[:-4])
        if len(data) > DEFAULT_HEADER_LIMIT_BYTES:
            raise EventSocketError("Event Socket header block is too large")


def _parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip() or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip()] = unquote(value.strip())
    return headers


def _content_length(headers: dict[str, str]) -> int:
    raw_value = _get_header(headers, "Content-Length")
    if raw_value is None:
        return 0
    try:
        return int(raw_value)
    except ValueError as err:
        raise EventSocketError("invalid Event Socket Content-Length") from err


def _parse_event_json(
    headers: dict[str, str],
    body: str,
) -> dict[str, Any] | None:
    candidates = [
        body,
        _get_header(headers, "Event-Body"),
        _get_header(headers, "Body"),
        _get_header(headers, "Data"),
        _get_header(headers, "Event-Data"),
        _get_header(headers, "Application-Data"),
        _get_header(headers, "Playback-Data"),
        _get_header(headers, "Playback-Event"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        if not candidate:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
    event_type = _first_header(
        headers,
        "Playback-Event",
        "Audio-Stream-Event",
        "Event",
    )
    if event_type:
        payload = {"event": event_type}
        for name in ("seq", "size", "remaining", "total_chunks"):
            value = _get_header(headers, name)
            if value is not None:
                payload[name] = value
        return payload
    return None


def _get_header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _first_header(headers: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = _get_header(headers, name)
        if value:
            return value
    return None


def _command_ok(message: EventSocketMessage) -> bool:
    reply_text = _reply_text(message).strip()
    return reply_text.startswith("+OK")


def _reply_text(message: EventSocketMessage) -> str:
    body = message.body.strip()
    if body:
        return body
    return _get_header(message.headers, "Reply-Text") or ""


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
