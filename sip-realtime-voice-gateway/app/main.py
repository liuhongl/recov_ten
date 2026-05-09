from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
from dataclasses import asdict

from .config import load_config
from .freeswitch_media import FreeSwitchMediaEchoServer
from .health_server import HealthServer
from .logging_config import configure_logging

LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="SIP realtime voice gateway")
    parser.add_argument(
        "--config",
        default="configs/local.example.toml",
        help="Path to TOML config file",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Load config, print it, then exit",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging.level)

    if args.check_config:
        print(json.dumps(asdict(config), ensure_ascii=False, indent=2))
        return 0

    try:
        asyncio.run(_serve(config))
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested")
    return 0


async def _serve(config) -> None:
    health_server = HealthServer(config)
    health_thread = threading.Thread(
        target=health_server.serve_forever,
        name="gateway-health-server",
        daemon=True,
    )
    media_server = FreeSwitchMediaEchoServer(config.freeswitch)

    health_thread.start()
    try:
        await media_server.serve_forever()
    finally:
        health_server.shutdown()
        health_thread.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
