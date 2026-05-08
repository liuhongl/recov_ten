#!/usr/bin/env python3

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass

from websockets.legacy.client import connect


@dataclass
class FakeAudioStats:
    fs_sent_bytes: int = 0
    fs_received_bytes: int = 0
    ten_received_bytes: int = 0
    ten_sent_bytes: int = 0


def _pcm_chunk(seed: int, size: int) -> bytes:
    return bytes((seed + offset) % 256 for offset in range(size))


async def _fake_ten_client(
    url: str,
    channel: str,
    chunks: int,
    stats: FakeAudioStats,
) -> None:
    logger = logging.getLogger("fake_ten_client")
    async with connect(f"{url}/media/ten/{channel}", ping_interval=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "register",
                    "role": "ten",
                    "channel": channel,
                    "sample_rate": 8000,
                }
            )
        )

        received_chunks = 0
        while received_chunks < chunks:
            message = await ws.recv()
            if isinstance(message, str):
                logger.info("control type=%s", json.loads(message).get("type"))
                continue

            received_chunks += 1
            stats.ten_received_bytes += len(message)
            reply = message[::-1]
            await ws.send(reply)
            stats.ten_sent_bytes += len(reply)
            logger.info(
                "received_and_replied chunk=%s bytes=%s",
                received_chunks,
                len(message),
            )


async def _fake_fs_client(
    url: str,
    channel: str,
    chunks: int,
    chunk_size: int,
    stats: FakeAudioStats,
) -> None:
    logger = logging.getLogger("fake_fs_client")
    async with connect(f"{url}/media/fs/{channel}", ping_interval=None) as ws:
        received_chunks = 0
        sent_chunks = 0

        while sent_chunks < chunks:
            message = await ws.recv()
            if isinstance(message, str):
                logger.info("control type=%s", json.loads(message).get("type"))
                break

        while sent_chunks < chunks:
            payload = _pcm_chunk(sent_chunks, chunk_size)
            await ws.send(payload)
            sent_chunks += 1
            stats.fs_sent_bytes += len(payload)
            logger.info("sent chunk=%s bytes=%s", sent_chunks, len(payload))

            while True:
                message = await ws.recv()
                if isinstance(message, str):
                    logger.info(
                        "control type=%s", json.loads(message).get("type")
                    )
                    continue
                received_chunks += 1
                stats.fs_received_bytes += len(message)
                logger.info(
                    "received chunk=%s bytes=%s",
                    received_chunks,
                    len(message),
                )
                break


async def _main() -> None:
    args = _parse_args()
    _setup_logging()

    stats = FakeAudioStats()
    ten_task = asyncio.create_task(
        _fake_ten_client(args.url, args.channel, args.chunks, stats)
    )
    await asyncio.sleep(0.2)
    fs_task = asyncio.create_task(
        _fake_fs_client(
            args.url,
            args.channel,
            args.chunks,
            args.chunk_size,
            stats,
        )
    )

    await asyncio.wait_for(
        asyncio.gather(ten_task, fs_task),
        timeout=args.timeout,
    )

    expected_bytes = args.chunks * args.chunk_size
    if (
        stats.fs_sent_bytes != expected_bytes
        or stats.ten_received_bytes != expected_bytes
        or stats.ten_sent_bytes != expected_bytes
        or stats.fs_received_bytes != expected_bytes
    ):
        raise RuntimeError(f"unexpected fake audio stats: {stats}")

    logging.getLogger("fake_audio_test").info(
        "fake_audio_roundtrip_ok channel=%s chunks=%s bytes_each_way=%s",
        args.channel,
        args.chunks,
        expected_bytes,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fake FS/TEN clients for Media Hub stage 3 validation",
    )
    parser.add_argument("--url", default="ws://127.0.0.1:9000")
    parser.add_argument("--channel", default="call_fake_audio_001")
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=320)
    parser.add_argument("--timeout", type=int, default=10)
    return parser.parse_args()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


if __name__ == "__main__":
    asyncio.run(_main())
