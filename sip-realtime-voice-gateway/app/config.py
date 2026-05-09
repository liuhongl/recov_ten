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
    echo_mode: str = "raw"


@dataclass(frozen=True)
class RealtimeConfig:
    provider: str = "aliyun"
    url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    model: str = "qwen3.5-omni-plus-realtime"
    voice: str = "Ethan"


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
    realtime: RealtimeConfig = RealtimeConfig()
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
            echo_mode=_get(
                raw,
                "freeswitch",
                "echo_mode",
                default=FreeSwitchConfig.echo_mode,
            ),
        ),
        realtime=RealtimeConfig(
            provider=_get(
                raw,
                "realtime",
                "provider",
                default=RealtimeConfig.provider,
            ),
            url=_get(raw, "realtime", "url", default=RealtimeConfig.url),
            model=_get(raw, "realtime", "model", default=RealtimeConfig.model),
            voice=_get(raw, "realtime", "voice", default=RealtimeConfig.voice),
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
    return _apply_env_overrides(config)


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
            echo_mode=os.getenv(
                "FREESWITCH_ECHO_MODE",
                config.freeswitch.echo_mode,
            ),
        ),
        realtime=RealtimeConfig(
            provider=os.getenv("REALTIME_MODEL_PROVIDER", config.realtime.provider),
            url=os.getenv("ALIYUN_REALTIME_URL", config.realtime.url),
            model=os.getenv("ALIYUN_REALTIME_MODEL", config.realtime.model),
            voice=os.getenv("ALIYUN_REALTIME_VOICE", config.realtime.voice),
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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _parse_bool(value, name)
