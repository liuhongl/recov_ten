#!/usr/bin/env python3

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass

from websockets.legacy.client import WebSocketClientProtocol, connect


@dataclass
class FakeFsStats:
    sent_bytes: int = 0
    received_bytes: int = 0


def _pcm_chunk(seed: int, size: int) -> bytes:
    return bytes((seed + offset) % 256 for offset in range(size))


async def _wait_for_media_connected(
    ws: WebSocketClientProtocol,
    timeout: int,
) -> None:
    logger = logging.getLogger("fake_fs_audio_client")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for media_connected")

        message = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if not isinstance(message, str):
            logger.info("ignoring unexpected binary before pairing bytes=%s", len(message))
            continue

        payload = json.loads(message)
        logger.info("control type=%s", payload.get("type"))
        if payload.get("type") == "media_connected":
            return


async def _run_fake_fs_client(args: argparse.Namespace) -> None:
    logger = logging.getLogger("fake_fs_audio_client")
    stats = FakeFsStats()

    async with connect(f"{args.url}/media/fs/{args.channel}", ping_interval=None) as ws:
        await _wait_for_media_connected(ws, args.timeout)

        for index in range(args.chunks):
            expected = _pcm_chunk(index, args.chunk_size)
            await ws.send(expected)
            stats.sent_bytes += len(expected)
            logger.info("sent chunk=%s bytes=%s", index + 1, len(expected))

            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                if isinstance(message, str):
                    logger.info("control type=%s", json.loads(message).get("type"))
                    continue

                actual = bytes(message)
                if actual != expected:
                    raise RuntimeError(
                        "unexpected echo payload for chunk "
                        f"{index + 1}: expected={len(expected)} actual={len(actual)}"
                    )

                stats.received_bytes += len(actual)
                logger.info("received echo chunk=%s bytes=%s", index + 1, len(actual))
                break

    expected_bytes = args.chunks * args.chunk_size
    if stats.sent_bytes != expected_bytes or stats.received_bytes != expected_bytes:
        raise RuntimeError(f"unexpected fake fs stats: {stats}")

    logger.info(
        "fake_fs_audio_roundtrip_ok channel=%s chunks=%s bytes_each_way=%s",
        args.channel,
        args.chunks,
        expected_bytes,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fake FS client for sip_media_bridge AudioFrame roundtrip",
    )
    parser.add_argument("--url", default="ws://127.0.0.1:9000")
    parser.add_argument("--channel", default="call_stage4_audio_frame_001")
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=320)
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def _main() -> None:
    args = _parse_args()
    _setup_logging()
    await _run_fake_fs_client(args)


if __name__ == "__main__":
    asyncio.run(_main())
