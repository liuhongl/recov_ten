#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import json
import logging
import math
import struct
import sys
import time
from dataclasses import dataclass
from typing import Literal

from websockets.exceptions import ConnectionClosedOK
from websockets.legacy.client import connect

ResponseMode = Literal["echo", "tone"]


@dataclass
class ResponderStats:
    fs_received_chunks: int = 0
    fs_received_bytes: int = 0
    ten_sent_chunks: int = 0
    ten_sent_bytes: int = 0


class ToneGenerator:
    def __init__(self, sample_rate: int, hz: float, amplitude: float) -> None:
        self.sample_rate = sample_rate
        self.hz = hz
        self.amplitude = amplitude
        self._sample_index = 0

    def next_frame(self, frame_ms: int) -> bytes:
        samples = int(self.sample_rate * frame_ms / 1000)
        frame = bytearray()
        for _ in range(samples):
            value = int(
                32767
                * self.amplitude
                * math.sin(
                    2
                    * math.pi
                    * self.hz
                    * self._sample_index
                    / self.sample_rate
                )
            )
            frame.extend(struct.pack("<h", value))
            self._sample_index += 1
        return bytes(frame)


async def _main() -> None:
    args = _parse_args()
    _setup_logging()

    logger = logging.getLogger("fake_ten_audio_responder")
    stats = ResponderStats()
    tone = ToneGenerator(args.sample_rate, args.tone_hz, args.amplitude)
    frame_interval = args.frame_ms / 1000
    initial_tone_sent = False

    async with connect(
        f"{args.url}/media/ten/{args.channel}",
        ping_interval=None,
    ) as ws:
        heartbeat_task = asyncio.create_task(
            _send_heartbeats(ws, args.channel, args.heartbeat_interval)
        )
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "role": "ten",
                        "channel": args.channel,
                        "sample_rate": args.sample_rate,
                    }
                )
            )
            logger.info(
                "responder_ready channel=%s sample_rate=%s frame_ms=%s "
                "mode=%s",
                args.channel,
                args.sample_rate,
                args.frame_ms,
                args.mode,
            )

            try:
                while stats.fs_received_chunks < args.max_inbound_chunks:
                    message = await asyncio.wait_for(
                        ws.recv(),
                        timeout=args.timeout,
                    )
                    if isinstance(message, str):
                        message_type = json.loads(message).get("type")
                        logger.info(
                            "control type=%s",
                            message_type,
                        )
                        if (
                            args.initial_tone
                            and message_type == "media_connected"
                            and not initial_tone_sent
                        ):
                            await _send_tone_frames(
                                ws,
                                tone,
                                stats,
                                args.initial_frames,
                                args.frame_ms,
                                frame_interval,
                            )
                            initial_tone_sent = True
                        continue

                    stats.fs_received_chunks += 1
                    stats.fs_received_bytes += len(message)
                    logger.info(
                        "fs_audio_received chunk=%s bytes=%s",
                        stats.fs_received_chunks,
                        len(message),
                    )

                    if args.mode == "echo":
                        await ws.send(message)
                        stats.ten_sent_chunks += 1
                        stats.ten_sent_bytes += len(message)
                        logger.info(
                            "echo_sent chunk=%s bytes=%s",
                            stats.ten_sent_chunks,
                            len(message),
                        )
                    else:
                        await _send_tone_frames(
                            ws,
                            tone,
                            stats,
                            args.response_frames_per_inbound,
                            args.frame_ms,
                            frame_interval,
                        )
            except ConnectionClosedOK:
                logger.info(
                    "websocket_closed_normally channel=%s",
                    args.channel,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "responder_timeout channel=%s timeout=%s",
                    args.channel,
                    args.timeout,
                )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    logger.info(
        "responder_done channel=%s fs_received_chunks=%s "
        "fs_received_bytes=%s ten_sent_chunks=%s ten_sent_bytes=%s",
        args.channel,
        stats.fs_received_chunks,
        stats.fs_received_bytes,
        stats.ten_sent_chunks,
        stats.ten_sent_bytes,
    )


async def _send_tone_frames(
    ws,
    tone: ToneGenerator,
    stats: ResponderStats,
    frames: int,
    frame_ms: int,
    frame_interval: float,
) -> None:
    logger = logging.getLogger("fake_ten_audio_responder")
    for _ in range(frames):
        frame = tone.next_frame(frame_ms)
        await ws.send(frame)
        stats.ten_sent_chunks += 1
        stats.ten_sent_bytes += len(frame)
        logger.info(
            "tone_sent chunk=%s bytes=%s",
            stats.ten_sent_chunks,
            len(frame),
        )
        await asyncio.sleep(frame_interval)


async def _send_heartbeats(ws, channel: str, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(
            json.dumps(
                {
                    "type": "ping",
                    "role": "ten",
                    "channel": channel,
                    "ts": time.time(),
                }
            )
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fake TEN-side responder for FreeSWITCH mod_audio_stream tests"
        ),
    )
    parser.add_argument("--url", default="ws://127.0.0.1:9000")
    parser.add_argument("--channel", default="fs_stage5a_local")
    parser.add_argument("--sample-rate", type=int, default=8000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--tone-hz", type=float, default=440.0)
    parser.add_argument("--amplitude", type=float, default=0.25)
    parser.add_argument("--mode", choices=["echo", "tone"], default="echo")
    parser.add_argument("--initial-tone", action="store_true")
    parser.add_argument("--initial-frames", type=int, default=40)
    parser.add_argument("--response-frames-per-inbound", type=int, default=1)
    parser.add_argument("--max-inbound-chunks", type=int, default=80)
    parser.add_argument("--heartbeat-interval", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


if __name__ == "__main__":
    asyncio.run(_main())
