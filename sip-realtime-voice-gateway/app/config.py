from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 9100


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class FreeSwitchConfig:
    media_host: str = "0.0.0.0"
    media_port: int = 9101
    sample_rate: int = 8000
    phone_codec: str = "PCMA"
    channels: int = 1
    frame_duration_ms: int = 20
    echo_mode: str = "raw"


@dataclass(frozen=True)
class EventSocketConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18021
    password_env: str = "FREESWITCH_ESL_PASSWORD"


@dataclass(frozen=True)
class DoubaoS2SConfig:
    app_id_env: str = "DOUBAO_S2S_APP_ID"
    access_token_env: str = "DOUBAO_S2S_ACCESS_TOKEN"
    app_key_env: str = "DOUBAO_S2S_APP_KEY"
    resource_id: str = "volc.speech.dialog"
    websocket_url: str = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
    speaker: str = "zh_female_vv_jupiter_bigtts"
    output_sample_rate: int = 24000


@dataclass(frozen=True)
class ServerVadConfig:
    type: str = "server_vad"
    threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 800
    create_response: bool = True
    interrupt_response: bool = True


@dataclass(frozen=True)
class PlaybackConfig:
    jitter_buffer_ms: int = 240
    send_interval_ms: int = 10
    tail_silence_ms: int = 300


@dataclass(frozen=True)
class VadConfig:
    speech_rms_threshold: int = 300
    start_speech_ms: int = 60
    end_silence_ms: int = 500
    min_speech_ms: int = 240
    max_utterance_ms: int = 12000
    pre_speech_ms: int = 160
    keep_silence_ms: int = 160
    barge_in_enabled: bool = False


@dataclass(frozen=True)
class FeatureConfig:
    metrics_enabled: bool = True
    recording_enabled: bool = False


@dataclass(frozen=True)
class GatewayConfig:
    server: ServerConfig = ServerConfig()
    logging: LoggingConfig = LoggingConfig()
    freeswitch: FreeSwitchConfig = FreeSwitchConfig()
    event_socket: EventSocketConfig = EventSocketConfig()
    doubao_s2s: DoubaoS2SConfig = DoubaoS2SConfig()
    server_vad: ServerVadConfig = ServerVadConfig()
    playback: PlaybackConfig = PlaybackConfig()
    vad: VadConfig = VadConfig()
    features: FeatureConfig = FeatureConfig()


def load_config(path: str | Path | None = None) -> GatewayConfig:
    raw = _load_toml(path)
    config = GatewayConfig(
        server=ServerConfig(
            host=_get(raw, "server", "host", default=ServerConfig.host),
            port=_get_int(raw, "server", "port", default=ServerConfig.port),
        ),
        logging=LoggingConfig(
            level=_get(raw, "logging", "level", default=LoggingConfig.level),
        ),
        freeswitch=FreeSwitchConfig(
            media_host=_get(
                raw,
                "freeswitch",
                "media_host",
                default=FreeSwitchConfig.media_host,
            ),
            media_port=_get_int(
                raw,
                "freeswitch",
                "media_port",
                default=FreeSwitchConfig.media_port,
            ),
            sample_rate=_get_int(
                raw,
                "freeswitch",
                "sample_rate",
                default=FreeSwitchConfig.sample_rate,
            ),
            phone_codec=_get(
                raw,
                "freeswitch",
                "phone_codec",
                default=FreeSwitchConfig.phone_codec,
            ),
            channels=_get_int(
                raw,
                "freeswitch",
                "channels",
                default=FreeSwitchConfig.channels,
            ),
            frame_duration_ms=_get_int(
                raw,
                "freeswitch",
                "frame_duration_ms",
                default=FreeSwitchConfig.frame_duration_ms,
            ),
            echo_mode=_get(
                raw,
                "freeswitch",
                "echo_mode",
                default=FreeSwitchConfig.echo_mode,
            ),
        ),
        event_socket=EventSocketConfig(
            enabled=_get_bool(
                raw,
                "event_socket",
                "enabled",
                default=EventSocketConfig.enabled,
            ),
            host=_get(
                raw,
                "event_socket",
                "host",
                default=EventSocketConfig.host,
            ),
            port=_get_int(
                raw,
                "event_socket",
                "port",
                default=EventSocketConfig.port,
            ),
            password_env=_get(
                raw,
                "event_socket",
                "password_env",
                default=EventSocketConfig.password_env,
            ),
        ),
        doubao_s2s=DoubaoS2SConfig(
            app_id_env=_get(
                raw,
                "doubao_s2s",
                "app_id_env",
                default=DoubaoS2SConfig.app_id_env,
            ),
            access_token_env=_get(
                raw,
                "doubao_s2s",
                "access_token_env",
                default=DoubaoS2SConfig.access_token_env,
            ),
            app_key_env=_get(
                raw,
                "doubao_s2s",
                "app_key_env",
                default=DoubaoS2SConfig.app_key_env,
            ),
            resource_id=_get(
                raw,
                "doubao_s2s",
                "resource_id",
                default=DoubaoS2SConfig.resource_id,
            ),
            websocket_url=_get(
                raw,
                "doubao_s2s",
                "websocket_url",
                default=DoubaoS2SConfig.websocket_url,
            ),
            speaker=_get(
                raw,
                "doubao_s2s",
                "speaker",
                default=DoubaoS2SConfig.speaker,
            ),
            output_sample_rate=_get_int(
                raw,
                "doubao_s2s",
                "output_sample_rate",
                default=DoubaoS2SConfig.output_sample_rate,
            ),
        ),
        server_vad=ServerVadConfig(
            type=_get(raw, "server_vad", "type", default=ServerVadConfig.type),
            threshold=_get_float(
                raw,
                "server_vad",
                "threshold",
                default=ServerVadConfig.threshold,
            ),
            prefix_padding_ms=_get_int(
                raw,
                "server_vad",
                "prefix_padding_ms",
                default=ServerVadConfig.prefix_padding_ms,
            ),
            silence_duration_ms=_get_int(
                raw,
                "server_vad",
                "silence_duration_ms",
                default=ServerVadConfig.silence_duration_ms,
            ),
            create_response=_get_bool(
                raw,
                "server_vad",
                "create_response",
                default=ServerVadConfig.create_response,
            ),
            interrupt_response=_get_bool(
                raw,
                "server_vad",
                "interrupt_response",
                default=ServerVadConfig.interrupt_response,
            ),
        ),
        playback=PlaybackConfig(
            jitter_buffer_ms=_get_int(
                raw,
                "playback",
                "jitter_buffer_ms",
                default=PlaybackConfig.jitter_buffer_ms,
            ),
            send_interval_ms=_get_int(
                raw,
                "playback",
                "send_interval_ms",
                default=PlaybackConfig.send_interval_ms,
            ),
            tail_silence_ms=_get_int(
                raw,
                "playback",
                "tail_silence_ms",
                default=PlaybackConfig.tail_silence_ms,
            ),
        ),
        vad=VadConfig(
            speech_rms_threshold=_get_int(
                raw,
                "vad",
                "speech_rms_threshold",
                default=VadConfig.speech_rms_threshold,
            ),
            start_speech_ms=_get_int(
                raw,
                "vad",
                "start_speech_ms",
                default=VadConfig.start_speech_ms,
            ),
            end_silence_ms=_get_int(
                raw,
                "vad",
                "end_silence_ms",
                default=VadConfig.end_silence_ms,
            ),
            min_speech_ms=_get_int(
                raw,
                "vad",
                "min_speech_ms",
                default=VadConfig.min_speech_ms,
            ),
            max_utterance_ms=_get_int(
                raw,
                "vad",
                "max_utterance_ms",
                default=VadConfig.max_utterance_ms,
            ),
            pre_speech_ms=_get_int(
                raw,
                "vad",
                "pre_speech_ms",
                default=VadConfig.pre_speech_ms,
            ),
            keep_silence_ms=_get_int(
                raw,
                "vad",
                "keep_silence_ms",
                default=VadConfig.keep_silence_ms,
            ),
            barge_in_enabled=_get_bool(
                raw,
                "vad",
                "barge_in_enabled",
                default=VadConfig.barge_in_enabled,
            ),
        ),
        features=FeatureConfig(
            metrics_enabled=_get_bool(
                raw,
                "features",
                "metrics_enabled",
                default=FeatureConfig.metrics_enabled,
            ),
            recording_enabled=_get_bool(
                raw,
                "features",
                "recording_enabled",
                default=FeatureConfig.recording_enabled,
            ),
        ),
    )
    config = _apply_env_overrides(config)
    _validate_media_contract(config.freeswitch)
    return config


def _load_toml(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    with config_path.open("rb") as file:
        data = tomllib.load(file)
    if not isinstance(data, dict):
        raise ValueError("config root must be a table")
    return data


def _get(
    raw: dict[str, Any],
    section: str,
    name: str,
    *,
    default: str,
) -> str:
    value = raw.get(section, {}).get(name, default)
    return str(value)


def _get_int(
    raw: dict[str, Any],
    section: str,
    name: str,
    *,
    default: int,
) -> int:
    value = raw.get(section, {}).get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{section}.{name} must be an integer") from err


def _get_float(
    raw: dict[str, Any],
    section: str,
    name: str,
    *,
    default: float,
) -> float:
    value = raw.get(section, {}).get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{section}.{name} must be a float") from err


def _get_bool(
    raw: dict[str, Any],
    section: str,
    name: str,
    *,
    default: bool,
) -> bool:
    value = raw.get(section, {}).get(name, default)
    return _parse_bool(value, f"{section}.{name}")


def _parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _apply_env_overrides(config: GatewayConfig) -> GatewayConfig:
    return GatewayConfig(
        server=ServerConfig(
            host=os.getenv("GATEWAY_HOST", config.server.host),
            port=_env_int("GATEWAY_PORT", config.server.port),
        ),
        logging=LoggingConfig(
            level=os.getenv("LOG_LEVEL", config.logging.level),
        ),
        freeswitch=FreeSwitchConfig(
            media_host=os.getenv(
                "FREESWITCH_MEDIA_HOST",
                config.freeswitch.media_host,
            ),
            media_port=_env_int(
                "FREESWITCH_MEDIA_PORT",
                config.freeswitch.media_port,
            ),
            sample_rate=_env_int(
                "FREESWITCH_SAMPLE_RATE",
                config.freeswitch.sample_rate,
            ),
            phone_codec=os.getenv("PHONE_CODEC", config.freeswitch.phone_codec),
            channels=_env_int("FREESWITCH_CHANNELS", config.freeswitch.channels),
            frame_duration_ms=_env_int(
                "FREESWITCH_FRAME_DURATION_MS",
                config.freeswitch.frame_duration_ms,
            ),
            echo_mode=os.getenv(
                "FREESWITCH_ECHO_MODE",
                config.freeswitch.echo_mode,
            ),
        ),
        event_socket=EventSocketConfig(
            enabled=_env_bool(
                "FREESWITCH_ESL_ENABLED",
                config.event_socket.enabled,
            ),
            host=os.getenv(
                "FREESWITCH_ESL_HOST",
                config.event_socket.host,
            ),
            port=_env_int(
                "FREESWITCH_ESL_PORT",
                config.event_socket.port,
            ),
            password_env=os.getenv(
                "FREESWITCH_ESL_PASSWORD_ENV",
                config.event_socket.password_env,
            ),
        ),
        doubao_s2s=DoubaoS2SConfig(
            app_id_env=os.getenv(
                "DOUBAO_S2S_APP_ID_ENV",
                config.doubao_s2s.app_id_env,
            ),
            access_token_env=os.getenv(
                "DOUBAO_S2S_ACCESS_TOKEN_ENV",
                config.doubao_s2s.access_token_env,
            ),
            app_key_env=os.getenv(
                "DOUBAO_S2S_APP_KEY_ENV",
                config.doubao_s2s.app_key_env,
            ),
            resource_id=os.getenv(
                "DOUBAO_S2S_RESOURCE_ID",
                config.doubao_s2s.resource_id,
            ),
            websocket_url=os.getenv(
                "DOUBAO_S2S_WS_URL",
                config.doubao_s2s.websocket_url,
            ),
            speaker=os.getenv(
                "DOUBAO_S2S_SPEAKER",
                config.doubao_s2s.speaker,
            ),
            output_sample_rate=_env_int(
                "DOUBAO_S2S_OUTPUT_SAMPLE_RATE",
                config.doubao_s2s.output_sample_rate,
            ),
        ),
        server_vad=ServerVadConfig(
            type=os.getenv("SERVER_VAD_TYPE", config.server_vad.type),
            threshold=_env_float("SERVER_VAD_THRESHOLD", config.server_vad.threshold),
            prefix_padding_ms=_env_int(
                "SERVER_VAD_PREFIX_PADDING_MS",
                config.server_vad.prefix_padding_ms,
            ),
            silence_duration_ms=_env_int(
                "SERVER_VAD_SILENCE_DURATION_MS",
                config.server_vad.silence_duration_ms,
            ),
            create_response=_env_bool(
                "SERVER_VAD_CREATE_RESPONSE",
                config.server_vad.create_response,
            ),
            interrupt_response=_env_bool(
                "SERVER_VAD_INTERRUPT_RESPONSE",
                config.server_vad.interrupt_response,
            ),
        ),
        playback=PlaybackConfig(
            jitter_buffer_ms=_env_int(
                "PLAYBACK_JITTER_BUFFER_MS",
                config.playback.jitter_buffer_ms,
            ),
            send_interval_ms=_env_int(
                "PLAYBACK_SEND_INTERVAL_MS",
                config.playback.send_interval_ms,
            ),
            tail_silence_ms=_env_int(
                "PLAYOUT_TAIL_SILENCE_MS",
                config.playback.tail_silence_ms,
            ),
        ),
        vad=VadConfig(
            speech_rms_threshold=_env_int(
                "VAD_SPEECH_RMS_THRESHOLD",
                config.vad.speech_rms_threshold,
            ),
            start_speech_ms=_env_int(
                "VAD_START_SPEECH_MS",
                config.vad.start_speech_ms,
            ),
            end_silence_ms=_env_int(
                "VAD_END_SILENCE_MS",
                config.vad.end_silence_ms,
            ),
            min_speech_ms=_env_int(
                "VAD_MIN_SPEECH_MS",
                config.vad.min_speech_ms,
            ),
            max_utterance_ms=_env_int(
                "VAD_MAX_UTTERANCE_MS",
                config.vad.max_utterance_ms,
            ),
            pre_speech_ms=_env_int(
                "VAD_PRE_SPEECH_MS",
                config.vad.pre_speech_ms,
            ),
            keep_silence_ms=_env_int(
                "VAD_KEEP_SILENCE_MS",
                config.vad.keep_silence_ms,
            ),
            barge_in_enabled=_env_bool(
                "VAD_BARGE_IN_ENABLED",
                config.vad.barge_in_enabled,
            ),
        ),
        features=FeatureConfig(
            metrics_enabled=_env_bool(
                "METRICS_ENABLED",
                config.features.metrics_enabled,
            ),
            recording_enabled=_env_bool(
                "RECORDING_ENABLED",
                config.features.recording_enabled,
            ),
        ),
    )


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer") from err


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as err:
        raise ValueError(f"{name} must be a float") from err


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _parse_bool(value, name)


def _validate_media_contract(config: FreeSwitchConfig) -> None:
    from .media_contract import build_realtime_phone_contract

    build_realtime_phone_contract(config)
