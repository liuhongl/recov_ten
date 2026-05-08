#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServerProtocol, serve

Role = Literal["ten", "fs"]


@dataclass
class MediaSession:
    channel: str
    role: Role
    session_id: str
    websocket: WebSocketServerProtocol
    connected_at: float
    last_seen_at: float
    registered: bool = False
    sample_rate: int | None = None


class MediaHub:
    def __init__(self, heartbeat_timeout_seconds: int) -> None:
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.ten_sessions: dict[str, MediaSession] = {}
        self.fs_sessions: dict[str, MediaSession] = {}
        self.logger = logging.getLogger("media_hub")
        self._sweeper_task: asyncio.Task | None = None

    async def start(self, host: str, port: int) -> None:
        self._sweeper_task = asyncio.create_task(self._sweep_stale_sessions())
        try:
            async with serve(self._handle_connection, host, port):
                self.logger.info(
                    "media_hub started host=%s port=%s heartbeat_timeout_seconds=%s",
                    host,
                    port,
                    self.heartbeat_timeout_seconds,
                )
                await asyncio.Future()
        finally:
            if self._sweeper_task is not None:
                self._sweeper_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._sweeper_task

    async def _handle_connection(
        self,
        websocket: WebSocketServerProtocol,
        path: str,
    ) -> None:
        role_and_channel = self._role_and_channel_from_path(path)
        if role_and_channel is None:
            self.logger.warning("rejecting unsupported path path=%s", path)
            await websocket.close(code=1008, reason="unsupported path")
            return

        role, channel = role_and_channel
        session = MediaSession(
            channel=channel,
            role=role,
            session_id=uuid.uuid4().hex,
            websocket=websocket,
            connected_at=time.time(),
            last_seen_at=time.time(),
        )
        await self._store_session(session)
        self.logger.info(
            "%s_connected channel=%s session_id=%s peer=%s",
            role,
            channel,
            session.session_id,
            websocket.remote_address,
        )
        if self._session_is_ready(session):
            await self._notify_pair_if_ready(channel)

        try:
            await self._message_loop(session)
        except ConnectionClosed as err:
            self.logger.info(
                "%s_disconnected channel=%s session_id=%s code=%s",
                role,
                channel,
                session.session_id,
                err.code,
            )
        finally:
            await self._remove_session(session, "connection_closed")

    async def _message_loop(self, session: MediaSession) -> None:
        async for message in session.websocket:
            session.last_seen_at = time.time()
            if isinstance(message, bytes):
                await self._forward_audio(session, message)
                continue

            await self._handle_control_message(session, message)

    async def _handle_control_message(
        self,
        session: MediaSession,
        raw_message: str,
    ) -> None:
        message = json.loads(raw_message)
        message_type = message.get("type")

        if message_type == "register":
            await self._handle_register(session, message)
        elif message_type == "ping":
            await self._handle_ping(session, message)
        else:
            self.logger.info(
                "control_message role=%s channel=%s session_id=%s type=%s",
                session.role,
                session.channel,
                session.session_id,
                message_type,
            )

    async def _handle_register(
        self,
        session: MediaSession,
        message: dict,
    ) -> None:
        requested_channel = message.get("channel")
        if requested_channel != session.channel:
            await session.websocket.close(code=1008, reason="channel mismatch")
            return

        requested_role = message.get("role")
        if requested_role is not None and requested_role != session.role:
            await session.websocket.close(code=1008, reason="role mismatch")
            return

        session.registered = True
        session.sample_rate = message.get("sample_rate")
        self.logger.info(
            "%s_registered channel=%s session_id=%s sample_rate=%s",
            session.role,
            session.channel,
            session.session_id,
            session.sample_rate,
        )
        await session.websocket.send(
            json.dumps(
                {
                    "type": "registered",
                    "role": session.role,
                    "channel": session.channel,
                    "session_id": session.session_id,
                }
            )
        )
        await self._notify_pair_if_ready(session.channel)

    async def _handle_ping(
        self,
        session: MediaSession,
        message: dict,
    ) -> None:
        self.logger.info(
            "heartbeat role=%s channel=%s session_id=%s",
            session.role,
            session.channel,
            session.session_id,
        )
        await session.websocket.send(
            json.dumps(
                {
                    "type": "pong",
                    "role": session.role,
                    "channel": session.channel,
                    "session_id": session.session_id,
                    "ts": message.get("ts"),
                }
            )
        )

    async def _forward_audio(
        self,
        source: MediaSession,
        payload: bytes,
    ) -> None:
        peer = self._peer_session(source)
        direction = "fs_to_ten" if source.role == "fs" else "ten_to_fs"
        if peer is None:
            self.logger.warning(
                "audio_drop direction=%s channel=%s bytes=%s reason=no_peer",
                direction,
                source.channel,
                len(payload),
            )
            return

        await peer.websocket.send(payload)
        peer.last_seen_at = time.time()
        self.logger.info(
            "audio_forwarded direction=%s channel=%s bytes=%s",
            direction,
            source.channel,
            len(payload),
        )

    async def _notify_pair_if_ready(self, channel: str) -> None:
        ten = self.ten_sessions.get(channel)
        fs = self.fs_sessions.get(channel)
        if ten is None or fs is None:
            return
        if not self._session_is_ready(ten) or not self._session_is_ready(fs):
            return

        self.logger.info(
            "media_paired channel=%s ten_session_id=%s fs_session_id=%s",
            channel,
            ten.session_id,
            fs.session_id,
        )
        message = json.dumps(
            {
                "type": "media_connected",
                "channel": channel,
                "ten_session_id": ten.session_id,
                "fs_session_id": fs.session_id,
            }
        )
        await asyncio.gather(
            ten.websocket.send(message),
            fs.websocket.send(message),
            return_exceptions=True,
        )

    async def _sweep_stale_sessions(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for session in list(self._all_sessions()):
                age = now - session.last_seen_at
                if age <= self.heartbeat_timeout_seconds:
                    continue
                self.logger.warning(
                    "heartbeat_timeout role=%s channel=%s session_id=%s age=%.2f",
                    session.role,
                    session.channel,
                    session.session_id,
                    age,
                )
                await session.websocket.close(
                    code=1001,
                    reason="heartbeat timeout",
                )
                await self._remove_session(session, "heartbeat_timeout")

    async def _remove_session(
        self,
        session: MediaSession,
        reason: str,
    ) -> None:
        sessions = self._sessions_for_role(session.role)
        current = sessions.get(session.channel)
        if current is not session:
            return
        sessions.pop(session.channel, None)
        self.logger.info(
            "session_removed role=%s channel=%s session_id=%s reason=%s",
            session.role,
            session.channel,
            session.session_id,
            reason,
        )

        peer = self._peer_session(session)
        if peer is None:
            return
        with contextlib.suppress(ConnectionClosed):
            await peer.websocket.send(
                json.dumps(
                    {
                        "type": "peer_disconnected",
                        "channel": session.channel,
                        "peer_role": session.role,
                        "reason": reason,
                    }
                )
            )

    async def _store_session(self, session: MediaSession) -> None:
        sessions = self._sessions_for_role(session.role)
        current = sessions.get(session.channel)
        if current is not None and current is not session:
            self.logger.warning(
                "session_replaced role=%s channel=%s old_session_id=%s new_session_id=%s",
                session.role,
                session.channel,
                current.session_id,
                session.session_id,
            )
            with contextlib.suppress(ConnectionClosed):
                await current.websocket.close(
                    code=4000,
                    reason="replaced by new connection",
                )
        sessions[session.channel] = session

    def _sessions_for_role(self, role: Role) -> dict[str, MediaSession]:
        return self.ten_sessions if role == "ten" else self.fs_sessions

    def _peer_session(self, session: MediaSession) -> MediaSession | None:
        if session.role == "ten":
            return self.fs_sessions.get(session.channel)
        return self.ten_sessions.get(session.channel)

    def _session_is_ready(self, session: MediaSession) -> bool:
        if session.role == "fs":
            return True
        return session.registered

    def _all_sessions(self) -> list[MediaSession]:
        return list(self.ten_sessions.values()) + list(
            self.fs_sessions.values()
        )

    def _role_and_channel_from_path(self, path: str) -> tuple[Role, str] | None:
        for role in ("ten", "fs"):
            prefix = f"/media/{role}/"
            if not path.startswith(prefix):
                continue
            channel = path[len(prefix) :].strip("/")
            if channel:
                return role, channel
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Minimal Media Hub for SIP trunk staged validation",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--heartbeat-timeout", type=int, default=12)
    return parser.parse_args()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("/tmp/sip_trunk_media_hub.log"),
        ],
    )


async def _main() -> None:
    args = _parse_args()
    _setup_logging()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)

    hub = MediaHub(heartbeat_timeout_seconds=args.heartbeat_timeout)
    server_task = asyncio.create_task(hub.start(args.host, args.port))
    await stop_event.wait()
    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task


if __name__ == "__main__":
    asyncio.run(_main())
