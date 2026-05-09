from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict

from .config import load_config
from .env_loader import get_first_env, load_env_file
from .freeswitch_media import FreeSwitchMediaEchoServer
from .health_server import HealthServer
from .logging_config import configure_logging
from .realtime_phone_gateway import FreeSwitchRealtimeGatewayServer

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
    parser.add_argument(
        "--env-file",
        default="../ai_agents/.env",
        help="Optional env file used for API keys and overrides",
    )
    parser.add_argument(
        "--media-mode",
        choices=("echo", "realtime"),
        default=os.getenv("GATEWAY_MEDIA_MODE", "echo"),
        help="Media server mode. Use realtime for stage 5 phone-model loop.",
    )
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    config = load_config(args.config)
    configure_logging(config.logging.level)

    if args.check_config:
        print(json.dumps(asdict(config), ensure_ascii=False, indent=2))
        return 0

    try:
        asyncio.run(_serve(config, media_mode=args.media_mode))
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested")
    return 0


async def _serve(config, *, media_mode: str) -> None:
    health_server = HealthServer(config)
    health_thread = threading.Thread(
        target=health_server.serve_forever,
        name="gateway-health-server",
        daemon=True,
    )
    if media_mode == "echo":
        media_server = FreeSwitchMediaEchoServer(config.freeswitch)
    elif media_mode == "realtime":
        api_key = get_first_env(("DASHSCOPE_API_KEY", "ALIYUN_DASHSCOPE_API_KEY"))
        if not api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY or ALIYUN_DASHSCOPE_API_KEY is required "
                "for realtime media mode"
            )
        media_server = FreeSwitchRealtimeGatewayServer(config, api_key=api_key)
    else:
        raise ValueError(f"unsupported media_mode: {media_mode}")

    health_thread.start()
    try:
        await media_server.serve_forever()
    finally:
        health_server.shutdown()
        health_thread.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
