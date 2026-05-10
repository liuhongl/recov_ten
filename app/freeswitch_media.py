from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServer, WebSocketServerProtocol, serve

from .audio_codec import resample_pcm_s16le_mono
from .config import FreeSwitchConfig
from .media_contract import PhoneMediaContract

LOGGER = logging.getLogger(__name__)


@dataclass
class MediaSessionStats:
    call_id: str
    session_id: str
    sample_rate: int
    expected_frame_bytes: int
    connected_at: float
    last_seen_at: float
    inbound_frames: int = 0
    inbound_bytes: int = 0
    outbound_frames: int = 0
    outbound_bytes: int = 0
    invalid_frame_count: int = 0
    first_audio_at: float | None = None
    disconnected_at: float | None = None
    control_messages: list[str] = field(default_factory=list)


class FreeSwitchMediaEchoServer:
    """WebSocket media echo server used for FreeSWITCH loopback validation."""

    def __init__(
        self,
        config: FreeSwitchConfig,
        *,
        frame_duration_ms: int | None = None,
    ) -> None:
        self.config = config
        self.frame_duration_ms = (
            frame_duration_ms
            if frame_duration_ms is not None
            else config.frame_duration_ms
        )
        if config.echo_mode not in {"raw", "resample_16k_roundtrip"}:
            raise ValueError(f"unsupported freeswitch echo_mode: {config.echo_mode}")
        self.contract = PhoneMediaContract.from_config(
            config,
            frame_duration_ms=self.frame_duration_ms,
        )
        self.contract.validate_realtime_phone_contract()
        self.expected_frame_bytes = self.contract.pcm_frame_bytes
        self._server: WebSocketServer | None = None
        self._address: tuple[str, int] = (config.media_host, config.media_port)
        self.active_sessions: dict[str, MediaSessionStats] = {}
        self.completed_sessions: list[MediaSessionStats] = []

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    async def start(self) -> None:
        self._server = await serve(
            self._handle_connection,
            self.config.media_host,
            self.config.media_port,
            ping_interval=None,
        )
        if self._server.sockets:
            sockname = self._server.sockets[0].getsockname()
            self._address = (str(sockname[0]), int(sockname[1]))

        LOGGER.info(
            "freeswitch_media_echo_started host=%s port=%s sample_rate=%s "
            "frame_duration_ms=%s channels=%s phone_codec=%s "
            "expected_frame_bytes=%s encoded_payload_bytes=%s echo_mode=%s",
            self._address[0],
            self._address[1],
            self.contract.sample_rate,
            self.frame_duration_ms,
            self.contract.channels,
            self.contract.codec,
            self.expected_frame_bytes,
            self.contract.encoded_payload_bytes,
            self.config.echo_mode,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        LOGGER.info("freeswitch_media_echo_stopped")

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    async def _handle_connection(
        self,
        websocket: WebSocketServerProtocol,
    ) -> None:
        path = websocket.path
        call_id = self._call_id_from_path(path)
        if call_id is None:
            LOGGER.warning("rejecting_unsupported_media_path path=%s", path)
            await websocket.close(code=1008, reason="unsupported path")
            return

        now = time.time()
        session = MediaSessionStats(
            call_id=call_id,
            session_id=uuid.uuid4().hex,
            sample_rate=self.config.sample_rate,
            expected_frame_bytes=self.expected_frame_bytes,
            connected_at=now,
            last_seen_at=now,
        )
        self.active_sessions[session.session_id] = session
        LOGGER.info(
            "freeswitch_media_connected call_id=%s session_id=%s peer=%s",
            call_id,
            session.session_id,
            websocket.remote_address,
        )

        try:
            async for message in websocket:
                session.last_seen_at = time.time()
                if isinstance(message, bytes):
                    await self._echo_audio(websocket, session, message)
                    continue
                await self._handle_control_message(websocket, session, message)
        except ConnectionClosed as err:
            LOGGER.info(
                "freeswitch_media_disconnected call_id=%s session_id=%s code=%s",
                call_id,
                session.session_id,
                err.code,
            )
        finally:
            self._finish_session(session)

    async def _echo_audio(
        self,
        websocket: WebSocketServerProtocol,
        session: MediaSessionStats,
        payload: bytes,
    ) -> None:
        if session.first_audio_at is None:
            session.first_audio_at = time.time()
            LOGGER.info(
                "first_freeswitch_audio call_id=%s session_id=%s bytes=%s",
                session.call_id,
                session.session_id,
                len(payload),
            )

        session.inbound_frames += 1
        session.inbound_bytes += len(payload)

        if len(payload) != session.expected_frame_bytes:
            session.invalid_frame_count += 1
            LOGGER.warning(
                "freeswitch_audio_frame_size_mismatch call_id=%s "
                "session_id=%s bytes=%s expected=%s frame=%s",
                session.call_id,
                session.session_id,
                len(payload),
                session.expected_frame_bytes,
                session.inbound_frames,
            )

        echo_payload = self._echo_payload(payload)
        await websocket.send(echo_payload)
        session.outbound_frames += 1
        session.outbound_bytes += len(echo_payload)

        if session.inbound_frames <= 3 or session.inbound_frames % 50 == 0:
            LOGGER.info(
                "freeswitch_audio_echoed call_id=%s session_id=%s frame=%s "
                "bytes=%s echo_mode=%s",
                session.call_id,
                session.session_id,
                session.inbound_frames,
                len(echo_payload),
                self.config.echo_mode,
            )

    async def _handle_control_message(
        self,
        websocket: WebSocketServerProtocol,
        session: MediaSessionStats,
        raw_message: str,
    ) -> None:
        message_type = "unknown"
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw_message)
            if isinstance(payload, dict):
                message_type = str(payload.get("type", "unknown"))

        session.control_messages.append(message_type)
        LOGGER.info(
            "freeswitch_control_message call_id=%s session_id=%s type=%s",
            session.call_id,
            session.session_id,
            message_type,
        )

        if message_type == "ping":
            await websocket.send(
                json.dumps(
                    {
                        "type": "pong",
                        "call_id": session.call_id,
                        "session_id": session.session_id,
                    }
                )
            )

    def _echo_payload(self, payload: bytes) -> bytes:
        if self.config.echo_mode == "raw":
            return payload

        upsampled = resample_pcm_s16le_mono(
            payload,
            self.config.sample_rate,
            16000,
        )
        return resample_pcm_s16le_mono(upsampled, 16000, self.config.sample_rate)

    def _finish_session(self, session: MediaSessionStats) -> None:
        current = self.active_sessions.pop(session.session_id, None)
        if current is None:
            return
        session.disconnected_at = time.time()
        self.completed_sessions.append(session)
        LOGGER.info(
            "freeswitch_media_session_finished call_id=%s session_id=%s "
            "inbound_frames=%s inbound_bytes=%s outbound_frames=%s "
            "outbound_bytes=%s invalid_frame_count=%s duration_ms=%s",
            session.call_id,
            session.session_id,
            session.inbound_frames,
            session.inbound_bytes,
            session.outbound_frames,
            session.outbound_bytes,
            session.invalid_frame_count,
            int((session.disconnected_at - session.connected_at) * 1000),
        )

    @staticmethod
    def _call_id_from_path(path: str) -> str | None:
        prefixes = ("/media/fs/", "/media/")
        for prefix in prefixes:
            if not path.startswith(prefix):
                continue
            call_id = path[len(prefix) :].strip("/")
            if call_id:
                return call_id
        return None
