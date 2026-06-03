from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict, replace

from .browser_prompt_test import BrowserPromptTestStore
from .call_control import OutboundCallManager
from .config import load_config
from .env_loader import load_env_file
from .flow_callback import HttpFlowCallbackWriter, LoggingFlowCallbackWriter
from .freeswitch_media import FreeSwitchMediaEchoServer
from .handoff_transcript import (
    HttpHumanHandoffTranscriptProcessor,
    MockHumanHandoffTranscriptProcessor,
)
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

DOUBAO_DIALOG_FIELD_COMPAT_SYSTEM_PROMPT = (
    "请遵循 dialog.system_role 和 dialog.speaking_style 中的会话设定进行回复。"
)


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
    flow_callback_writer = None
    if config.flow_callback.enabled:
        flow_callback_writer = _build_flow_callback_writer(config)

    postgres_runtime = PostgresRuntime(
        config,
        fallback_instructions=DEFAULT_PHONE_INSTRUCTIONS,
        flow_callback_writer=flow_callback_writer,
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
    if postgres_runtime.prompt_store is not None:
        business_prompt_preparer = ThreadsafeBusinessPromptPreparer(
            asyncio.get_running_loop(),
            postgres_runtime.prompt_store,
            fallback_instructions=DEFAULT_PHONE_INSTRUCTIONS,
            timeout_seconds=config.postgres.command_timeout_seconds,
        )

    browser_prompt_store = BrowserPromptTestStore(
        business_prompt_preparer=business_prompt_preparer,
        opening_generator=opening_generator,
        opening_store=opening_store,
        config=config,
    )
    handoff_transcript_processor = _build_handoff_transcript_processor(config)
    outbound_manager = OutboundCallManager(
        config,
        opening_generator=opening_generator,
        opening_store=opening_store,
        business_prompt_preparer=business_prompt_preparer,
        call_record_updater=postgres_runtime.call_record_updater,
        call_result_writer=postgres_runtime.call_result_writer,
        flow_callback_writer=flow_callback_writer,
        destination_resolver=postgres_runtime.call_destination_resolver,
        handoff_transcript_processor=handoff_transcript_processor,
    )
    outbound_manager.start()
    health_server = HealthServer(
        config,
        call_manager=outbound_manager,
        browser_prompt_store=browser_prompt_store,
    )
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
            on_input_transcript,
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
                    system_prompt=_system_prompt_for_doubao_session(
                        instructions,
                        dialog_config,
                    ),
                    speaker=speaker or session_config.speaker,
                    dialog=dialog_config,
                ),
                turn_id_start=turn_id_start,
                on_speech_started=on_speech_started,
                on_input_transcript=on_input_transcript,
                on_audio_delta=on_audio_delta,
                on_turn_completed=on_turn_completed,
            )

        media_server = FreeSwitchRealtimeGatewayServer(
            config,
            api_key="doubao-s2s",
            model_output_sample_rate=config.doubao_s2s.output_sample_rate,
            realtime_session_factory=session_factory,
            prompt_store=postgres_runtime.prompt_store,
            prompt_snapshot_provider=_browser_first_prompt_snapshot_provider(
                browser_prompt_store,
                outbound_manager.get_prompt_snapshot,
            ),
            call_context_provider=outbound_manager.get_call_context,
            call_recording_path_provider=outbound_manager.get_call_recording_path,
            call_result_writer=postgres_runtime.call_result_writer,
            on_media_connected=outbound_manager.mark_media_connected,
            on_media_disconnected=outbound_manager.mark_media_disconnected,
            opening_store=opening_store,
            is_call_answered=outbound_manager.is_call_answered,
            handoff_requester=outbound_manager.request_handoff,
            agent_takeover_suggestion_recorder=(
                outbound_manager.record_agent_takeover_suggestion
            ),
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


def _build_flow_callback_writer(config):
    http = config.flow_callback.http
    if http.enabled:
        secret = os.getenv(http.secret_env, "")
        if not secret:
            raise RuntimeError(
                "missing flow callback HTTP secret in environment: " + http.secret_env
            )
        return HttpFlowCallbackWriter(
            base_url=http.base_url,
            path=http.path,
            client_id=http.client_id,
            secret=secret,
            timeout_seconds=http.timeout_seconds,
            max_attempts=http.max_attempts,
            retry_backoff_seconds=http.retry_backoff_seconds,
        )
    return LoggingFlowCallbackWriter(topic=config.flow_callback.topic)


def _build_handoff_transcript_processor(config):
    human_transcript = config.human_transcript
    if not human_transcript.enabled:
        return None
    if human_transcript.provider == "mock":
        return MockHumanHandoffTranscriptProcessor()
    if human_transcript.provider == "http_json":
        return HttpHumanHandoffTranscriptProcessor(
            human_transcript.http_url,
            timeout_seconds=human_transcript.timeout_seconds,
        )
    raise RuntimeError(
        f"unsupported human transcript provider: {human_transcript.provider}"
    )


def _system_prompt_for_doubao_session(
    instructions: str,
    dialog_config,
) -> str:
    if getattr(dialog_config, "system_role", None):
        return DOUBAO_DIALOG_FIELD_COMPAT_SYSTEM_PROMPT
    return instructions


def _browser_first_prompt_snapshot_provider(
    browser_prompt_store,
    outbound_provider,
):
    def provider(call_id: str):
        snapshot = browser_prompt_store.get(call_id)
        if snapshot is not None:
            return snapshot
        return outbound_provider(call_id)

    return provider


if __name__ == "__main__":
    raise SystemExit(main())
