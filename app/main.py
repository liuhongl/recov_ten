from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict, replace

from .call_control import OutboundCallManager
from .config import load_config
from .env_loader import load_env_file
from .freeswitch_media import FreeSwitchMediaEchoServer
from .health_server import HealthServer
from .logging_config import configure_logging
from .opening import (
    DEFAULT_OPENING_TIMEOUT_SECONDS,
    DoubaoOpeningAudioGenerator,
    OpeningAudioStore,
)
from .postgres import PostgresRuntime, ThreadsafeBusinessPromptPreparer
from .doubao_s2s_client import (
    DEFAULT_REALTIME_APP_KEY,
    DoubaoS2SCredentials,
    DoubaoS2SSessionConfig,
)
from .doubao_s2s_realtime import DoubaoS2SServerVadSession
from .realtime_phone_gateway import (
    DEFAULT_PHONE_INSTRUCTIONS,
    FreeSwitchRealtimeGatewayServer,
)

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
        default=".env",
        help="Optional env file used for API keys and overrides",
    )
    parser.add_argument(
        "--media-mode",
        choices=("echo", "realtime"),
        default=os.getenv("GATEWAY_MEDIA_MODE", "echo"),
        help="Media server mode. Use realtime for the Server VAD phone loop.",
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
    postgres_runtime = PostgresRuntime(
        config,
        fallback_instructions=DEFAULT_PHONE_INSTRUCTIONS,
    )
    await postgres_runtime.start()

    opening_store = OpeningAudioStore()
    doubao_credentials = None
    opening_generator = None
    if media_mode == "realtime":
        doubao_credentials = _load_doubao_s2s_credentials(config)
        opening_generator = DoubaoOpeningAudioGenerator(
            doubao_credentials,
            config.doubao_s2s,
            timeout_seconds=DEFAULT_OPENING_TIMEOUT_SECONDS,
        )

    business_prompt_preparer = None
    if postgres_runtime.prompt_store is not None and opening_generator is not None:
        business_prompt_preparer = ThreadsafeBusinessPromptPreparer(
            asyncio.get_running_loop(),
            postgres_runtime.prompt_store,
            fallback_instructions=DEFAULT_PHONE_INSTRUCTIONS,
            timeout_seconds=config.postgres.command_timeout_seconds,
        )

    outbound_manager = OutboundCallManager(
        config,
        opening_generator=opening_generator,
        opening_store=opening_store,
        business_prompt_preparer=business_prompt_preparer,
    )
    outbound_manager.start()
    health_server = HealthServer(config, call_manager=outbound_manager)
    health_thread = threading.Thread(
        target=health_server.serve_forever,
        name="gateway-health-server",
        daemon=True,
    )
    if media_mode == "echo":
        media_server = FreeSwitchMediaEchoServer(config.freeswitch)
    elif media_mode == "realtime":
        assert doubao_credentials is not None
        session_config = DoubaoS2SSessionConfig(
            speaker=config.doubao_s2s.speaker,
            output_sample_rate=config.doubao_s2s.output_sample_rate,
        )

        def session_factory(
            on_speech_started,
            on_audio_delta,
            on_turn_completed,
            turn_id_start,
            instructions,
            speaker,
            dialog_config,
        ):
            return DoubaoS2SServerVadSession(
                doubao_credentials,
                replace(
                    session_config,
                    system_prompt=instructions,
                    speaker=speaker or session_config.speaker,
                    dialog=dialog_config,
                ),
                turn_id_start=turn_id_start,
                on_speech_started=on_speech_started,
                on_audio_delta=on_audio_delta,
                on_turn_completed=on_turn_completed,
            )

        media_server = FreeSwitchRealtimeGatewayServer(
            config,
            api_key="doubao-s2s",
            model_output_sample_rate=config.doubao_s2s.output_sample_rate,
            realtime_session_factory=session_factory,
            prompt_store=postgres_runtime.prompt_store,
            prompt_snapshot_provider=outbound_manager.get_prompt_snapshot,
            call_result_writer=postgres_runtime.call_result_writer,
            on_media_connected=outbound_manager.mark_media_connected,
            on_media_disconnected=outbound_manager.mark_media_disconnected,
            opening_store=opening_store,
            is_call_answered=outbound_manager.is_call_answered,
        )
    else:
        raise ValueError(f"unsupported media_mode: {media_mode}")

    health_thread.start()
    try:
        await media_server.serve_forever()
    finally:
        health_server.shutdown()
        outbound_manager.shutdown()
        await postgres_runtime.stop()
        health_thread.join(timeout=3)


def _load_doubao_s2s_credentials(config) -> DoubaoS2SCredentials:
    doubao = config.doubao_s2s
    app_id = os.getenv(doubao.app_id_env, "")
    access_token = os.getenv(doubao.access_token_env, "")
    app_key = os.getenv(doubao.app_key_env) or DEFAULT_REALTIME_APP_KEY
    missing = []
    if not app_id:
        missing.append(doubao.app_id_env)
    if not access_token:
        missing.append(doubao.access_token_env)
    if missing:
        raise RuntimeError(
            "missing Doubao S2S credentials in environment: " + ", ".join(missing)
        )

    return DoubaoS2SCredentials(
        app_id=app_id,
        access_token=access_token,
        app_key=app_key,
        resource_id=doubao.resource_id,
        websocket_url=doubao.websocket_url,
    )


if __name__ == "__main__":
    raise SystemExit(main())
