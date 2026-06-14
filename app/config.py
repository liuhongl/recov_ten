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
class OutboundCallConfig:
    enabled: bool = True
    endpoint_template: str = "sofia_contact:*/{destination}"
    dialplan_extension: str = "9199"
    dialplan_context: str = "default"
    caller_id_name: str = "AI_Assistant"
    caller_id_number: str = "9000"
    originate_timeout_seconds: int = 30
    max_recent_calls: int = 200


@dataclass(frozen=True)
class RealtimeProviderConfig:
    provider: str = "doubao_s2s"


@dataclass(frozen=True)
class OpeningAudioProviderConfig:
    provider: str = "doubao_tts"


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
class DoubaoTTSConfig:
    enabled: bool = True
    app_id_env: str = "DOUBAO_S2S_APP_ID"
    access_token_env: str = "DOUBAO_S2S_ACCESS_TOKEN"
    api_key_env: str = "DOUBAO_TTS_API_KEY"
    resource_id: str = "seed-tts-2.0"
    endpoint: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    female_speaker: str = "zh_female_xiaohe_uranus_bigtts"
    male_speaker: str = "zh_male_m191_uranus_bigtts"
    audio_format: str = "pcm"
    output_sample_rate: int = 24000
    timeout_seconds: float = 10.0
    fallback_to_s2s: bool = True


@dataclass(frozen=True)
class QwenOmniRealtimeConfig:
    api_key_env: str = "DASHSCOPE_API_KEY"
    websocket_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    model: str = "qwen3-omni-flash-realtime"
    voice: str = "Cherry"
    output_sample_rate: int = 24000


@dataclass(frozen=True)
class QwenOpeningAudioConfig:
    api_key_env: str = "DASHSCOPE_API_KEY"
    endpoint: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    )
    model: str = "qwen3-tts-flash"
    voice: str = "Cherry"
    language_type: str = "Chinese"
    timeout_seconds: float = 30.0


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
    recording_dir: str = "/tmp/recov_ten_handoff_recordings"
    recording_host_dir: str = ""
    inbound_rms_diagnostics_enabled: bool = False


@dataclass(frozen=True)
class CallRecordingConfig:
    enabled: bool = False
    directory: str = "/var/lib/freeswitch/recordings"
    upload_enabled: bool = False
    host_directory: str = ""
    object_prefix: str = "recordings"
    upload_timeout_seconds: float = 30.0
    opening_warmup_ms: int = 600
    opening_source_debug_enabled: bool = False


@dataclass(frozen=True)
class PostgresConfig:
    enabled: bool = False
    dsn_env: str = "POSTGRES_DSN"
    min_pool_size: int = 1
    max_pool_size: int = 5
    command_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class HumanTranscriptConfig:
    enabled: bool = False
    provider: str = "http_json"
    http_url: str = ""
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class HandoffConfig:
    wait_timeout_seconds: int = 180


@dataclass(frozen=True)
class FlowCallbackHttpConfig:
    enabled: bool = False
    base_url: str = ""
    path: str = "/system/recov/flow/external/callback"
    client_id: str = "python-ai-call"
    secret_env: str = "FLOW_CALLBACK_HMAC_SECRET_AI_CALL"
    timeout_seconds: float = 10.0
    max_attempts: int = 1
    retry_backoff_seconds: float = 0.2


@dataclass(frozen=True)
class FlowCallbackConfig:
    enabled: bool = False
    topic: str = "recov-flow-callback"
    producer_group: str = "recov-ten-gateway"
    http: FlowCallbackHttpConfig = FlowCallbackHttpConfig()


@dataclass(frozen=True)
class RocketMQAclConfig:
    enabled: bool = False
    access_key_env: str = "ROCKETMQ_ACCESS_KEY"
    secret_key_env: str = "ROCKETMQ_SECRET_KEY"
    security_token_env: str = "ROCKETMQ_SECURITY_TOKEN"


@dataclass(frozen=True)
class RocketMQConfig:
    enabled: bool = False
    endpoint: str = "http://118.89.137.44/"
    name_server: str = "118.89.137.44:9876"
    producer_group: str = "recov-ten-gateway"
    callback_topic: str = "recov-flow-callback"
    send_timeout_ms: int = 3000
    acl: RocketMQAclConfig = RocketMQAclConfig()


@dataclass(frozen=True)
class GatewayConfig:
    server: ServerConfig = ServerConfig()
    logging: LoggingConfig = LoggingConfig()
    freeswitch: FreeSwitchConfig = FreeSwitchConfig()
    event_socket: EventSocketConfig = EventSocketConfig()
    outbound: OutboundCallConfig = OutboundCallConfig()
    realtime: RealtimeProviderConfig = RealtimeProviderConfig()
    opening_audio: OpeningAudioProviderConfig = OpeningAudioProviderConfig()
    doubao_s2s: DoubaoS2SConfig = DoubaoS2SConfig()
    doubao_tts: DoubaoTTSConfig = DoubaoTTSConfig()
    qwen_omni_realtime: QwenOmniRealtimeConfig = QwenOmniRealtimeConfig()
    qwen_opening_audio: QwenOpeningAudioConfig = QwenOpeningAudioConfig()
    server_vad: ServerVadConfig = ServerVadConfig()
    playback: PlaybackConfig = PlaybackConfig()
    vad: VadConfig = VadConfig()
    features: FeatureConfig = FeatureConfig()
    call_recording: CallRecordingConfig = CallRecordingConfig()
    postgres: PostgresConfig = PostgresConfig()
    human_transcript: HumanTranscriptConfig = HumanTranscriptConfig()
    handoff: HandoffConfig = HandoffConfig()
    flow_callback: FlowCallbackConfig = FlowCallbackConfig()
    rocketmq: RocketMQConfig = RocketMQConfig()


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
        outbound=OutboundCallConfig(
            enabled=_get_bool(
                raw,
                "outbound",
                "enabled",
                default=OutboundCallConfig.enabled,
            ),
            endpoint_template=_get(
                raw,
                "outbound",
                "endpoint_template",
                default=OutboundCallConfig.endpoint_template,
            ),
            dialplan_extension=_get(
                raw,
                "outbound",
                "dialplan_extension",
                default=OutboundCallConfig.dialplan_extension,
            ),
            dialplan_context=_get(
                raw,
                "outbound",
                "dialplan_context",
                default=OutboundCallConfig.dialplan_context,
            ),
            caller_id_name=_get(
                raw,
                "outbound",
                "caller_id_name",
                default=OutboundCallConfig.caller_id_name,
            ),
            caller_id_number=_get(
                raw,
                "outbound",
                "caller_id_number",
                default=OutboundCallConfig.caller_id_number,
            ),
            originate_timeout_seconds=_get_int(
                raw,
                "outbound",
                "originate_timeout_seconds",
                default=OutboundCallConfig.originate_timeout_seconds,
            ),
            max_recent_calls=_get_int(
                raw,
                "outbound",
                "max_recent_calls",
                default=OutboundCallConfig.max_recent_calls,
            ),
        ),
        realtime=RealtimeProviderConfig(
            provider=_get(
                raw,
                "realtime",
                "provider",
                default=RealtimeProviderConfig.provider,
            ),
        ),
        opening_audio=OpeningAudioProviderConfig(
            provider=_get(
                raw,
                "opening_audio",
                "provider",
                default=OpeningAudioProviderConfig.provider,
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
        doubao_tts=DoubaoTTSConfig(
            enabled=_get_bool(
                raw,
                "doubao_tts",
                "enabled",
                default=DoubaoTTSConfig.enabled,
            ),
            app_id_env=_get(
                raw,
                "doubao_tts",
                "app_id_env",
                default=DoubaoTTSConfig.app_id_env,
            ),
            access_token_env=_get(
                raw,
                "doubao_tts",
                "access_token_env",
                default=DoubaoTTSConfig.access_token_env,
            ),
            api_key_env=_get(
                raw,
                "doubao_tts",
                "api_key_env",
                default=DoubaoTTSConfig.api_key_env,
            ),
            resource_id=_get(
                raw,
                "doubao_tts",
                "resource_id",
                default=DoubaoTTSConfig.resource_id,
            ),
            endpoint=_get(
                raw,
                "doubao_tts",
                "endpoint",
                default=DoubaoTTSConfig.endpoint,
            ),
            female_speaker=_get(
                raw,
                "doubao_tts",
                "female_speaker",
                default=DoubaoTTSConfig.female_speaker,
            ),
            male_speaker=_get(
                raw,
                "doubao_tts",
                "male_speaker",
                default=DoubaoTTSConfig.male_speaker,
            ),
            audio_format=_get(
                raw,
                "doubao_tts",
                "audio_format",
                default=DoubaoTTSConfig.audio_format,
            ),
            output_sample_rate=_get_int(
                raw,
                "doubao_tts",
                "output_sample_rate",
                default=DoubaoTTSConfig.output_sample_rate,
            ),
            timeout_seconds=_get_float(
                raw,
                "doubao_tts",
                "timeout_seconds",
                default=DoubaoTTSConfig.timeout_seconds,
            ),
            fallback_to_s2s=_get_bool(
                raw,
                "doubao_tts",
                "fallback_to_s2s",
                default=DoubaoTTSConfig.fallback_to_s2s,
            ),
        ),
        qwen_omni_realtime=QwenOmniRealtimeConfig(
            api_key_env=_get(
                raw,
                "qwen_omni_realtime",
                "api_key_env",
                default=QwenOmniRealtimeConfig.api_key_env,
            ),
            websocket_url=_get(
                raw,
                "qwen_omni_realtime",
                "websocket_url",
                default=QwenOmniRealtimeConfig.websocket_url,
            ),
            model=_get(
                raw,
                "qwen_omni_realtime",
                "model",
                default=QwenOmniRealtimeConfig.model,
            ),
            voice=_get(
                raw,
                "qwen_omni_realtime",
                "voice",
                default=QwenOmniRealtimeConfig.voice,
            ),
            output_sample_rate=_get_int(
                raw,
                "qwen_omni_realtime",
                "output_sample_rate",
                default=QwenOmniRealtimeConfig.output_sample_rate,
            ),
        ),
        qwen_opening_audio=QwenOpeningAudioConfig(
            api_key_env=_get(
                raw,
                "qwen_opening_audio",
                "api_key_env",
                default=QwenOpeningAudioConfig.api_key_env,
            ),
            endpoint=_get(
                raw,
                "qwen_opening_audio",
                "endpoint",
                default=QwenOpeningAudioConfig.endpoint,
            ),
            model=_get(
                raw,
                "qwen_opening_audio",
                "model",
                default=QwenOpeningAudioConfig.model,
            ),
            voice=_get(
                raw,
                "qwen_opening_audio",
                "voice",
                default=QwenOpeningAudioConfig.voice,
            ),
            language_type=_get(
                raw,
                "qwen_opening_audio",
                "language_type",
                default=QwenOpeningAudioConfig.language_type,
            ),
            timeout_seconds=_get_float(
                raw,
                "qwen_opening_audio",
                "timeout_seconds",
                default=QwenOpeningAudioConfig.timeout_seconds,
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
            recording_dir=_get(
                raw,
                "features",
                "recording_dir",
                default=FeatureConfig.recording_dir,
            ),
            recording_host_dir=_get(
                raw,
                "features",
                "recording_host_dir",
                default=FeatureConfig.recording_host_dir,
            ),
            inbound_rms_diagnostics_enabled=_get_bool(
                raw,
                "features",
                "inbound_rms_diagnostics_enabled",
                default=FeatureConfig.inbound_rms_diagnostics_enabled,
            ),
        ),
        call_recording=CallRecordingConfig(
            enabled=_get_bool(
                raw,
                "call_recording",
                "enabled",
                default=CallRecordingConfig.enabled,
            ),
            directory=_get(
                raw,
                "call_recording",
                "directory",
                default=CallRecordingConfig.directory,
            ),
            upload_enabled=_get_bool(
                raw,
                "call_recording",
                "upload_enabled",
                default=CallRecordingConfig.upload_enabled,
            ),
            host_directory=_get(
                raw,
                "call_recording",
                "host_directory",
                default=CallRecordingConfig.host_directory,
            ),
            object_prefix=_get(
                raw,
                "call_recording",
                "object_prefix",
                default=CallRecordingConfig.object_prefix,
            ),
            upload_timeout_seconds=_get_float(
                raw,
                "call_recording",
                "upload_timeout_seconds",
                default=CallRecordingConfig.upload_timeout_seconds,
            ),
            opening_warmup_ms=_get_int(
                raw,
                "call_recording",
                "opening_warmup_ms",
                default=CallRecordingConfig.opening_warmup_ms,
            ),
            opening_source_debug_enabled=_get_bool(
                raw,
                "call_recording",
                "opening_source_debug_enabled",
                default=CallRecordingConfig.opening_source_debug_enabled,
            ),
        ),
        postgres=PostgresConfig(
            enabled=_get_bool(
                raw,
                "postgres",
                "enabled",
                default=PostgresConfig.enabled,
            ),
            dsn_env=_get(
                raw,
                "postgres",
                "dsn_env",
                default=PostgresConfig.dsn_env,
            ),
            min_pool_size=_get_int(
                raw,
                "postgres",
                "min_pool_size",
                default=PostgresConfig.min_pool_size,
            ),
            max_pool_size=_get_int(
                raw,
                "postgres",
                "max_pool_size",
                default=PostgresConfig.max_pool_size,
            ),
            command_timeout_seconds=_get_float(
                raw,
                "postgres",
                "command_timeout_seconds",
                default=PostgresConfig.command_timeout_seconds,
            ),
        ),
        human_transcript=HumanTranscriptConfig(
            enabled=_get_bool(
                raw,
                "human_transcript",
                "enabled",
                default=HumanTranscriptConfig.enabled,
            ),
            provider=_get(
                raw,
                "human_transcript",
                "provider",
                default=HumanTranscriptConfig.provider,
            ),
            http_url=_get(
                raw,
                "human_transcript",
                "http_url",
                default=HumanTranscriptConfig.http_url,
            ),
            timeout_seconds=_get_float(
                raw,
                "human_transcript",
                "timeout_seconds",
                default=HumanTranscriptConfig.timeout_seconds,
            ),
        ),
        handoff=HandoffConfig(
            wait_timeout_seconds=_get_int(
                raw,
                "handoff",
                "wait_timeout_seconds",
                default=HandoffConfig.wait_timeout_seconds,
            ),
        ),
        flow_callback=FlowCallbackConfig(
            enabled=_get_bool(
                raw,
                "flow_callback",
                "enabled",
                default=FlowCallbackConfig.enabled,
            ),
            topic=_get(
                raw,
                "flow_callback",
                "topic",
                default=FlowCallbackConfig.topic,
            ),
            producer_group=_get(
                raw,
                "flow_callback",
                "producer_group",
                default=FlowCallbackConfig.producer_group,
            ),
            http=FlowCallbackHttpConfig(
                enabled=_get_bool(
                    raw,
                    "flow_callback.http",
                    "enabled",
                    default=FlowCallbackHttpConfig.enabled,
                ),
                base_url=_get(
                    raw,
                    "flow_callback.http",
                    "base_url",
                    default=FlowCallbackHttpConfig.base_url,
                ),
                path=_get(
                    raw,
                    "flow_callback.http",
                    "path",
                    default=FlowCallbackHttpConfig.path,
                ),
                client_id=_get(
                    raw,
                    "flow_callback.http",
                    "client_id",
                    default=FlowCallbackHttpConfig.client_id,
                ),
                secret_env=_get(
                    raw,
                    "flow_callback.http",
                    "secret_env",
                    default=FlowCallbackHttpConfig.secret_env,
                ),
                timeout_seconds=_get_float(
                    raw,
                    "flow_callback.http",
                    "timeout_seconds",
                    default=FlowCallbackHttpConfig.timeout_seconds,
                ),
                max_attempts=_get_int(
                    raw,
                    "flow_callback.http",
                    "max_attempts",
                    default=FlowCallbackHttpConfig.max_attempts,
                ),
                retry_backoff_seconds=_get_float(
                    raw,
                    "flow_callback.http",
                    "retry_backoff_seconds",
                    default=FlowCallbackHttpConfig.retry_backoff_seconds,
                ),
            ),
        ),
        rocketmq=RocketMQConfig(
            enabled=_get_bool(
                raw,
                "rocketmq",
                "enabled",
                default=RocketMQConfig.enabled,
            ),
            endpoint=_get(
                raw,
                "rocketmq",
                "endpoint",
                default=RocketMQConfig.endpoint,
            ),
            name_server=_get(
                raw,
                "rocketmq",
                "name_server",
                default=RocketMQConfig.name_server,
            ),
            producer_group=_get(
                raw,
                "rocketmq",
                "producer_group",
                default=RocketMQConfig.producer_group,
            ),
            callback_topic=_get(
                raw,
                "rocketmq",
                "callback_topic",
                default=RocketMQConfig.callback_topic,
            ),
            send_timeout_ms=_get_int(
                raw,
                "rocketmq",
                "send_timeout_ms",
                default=RocketMQConfig.send_timeout_ms,
            ),
            acl=RocketMQAclConfig(
                enabled=_get_bool(
                    raw,
                    "rocketmq.acl",
                    "enabled",
                    default=RocketMQAclConfig.enabled,
                ),
                access_key_env=_get(
                    raw,
                    "rocketmq.acl",
                    "access_key_env",
                    default=RocketMQAclConfig.access_key_env,
                ),
                secret_key_env=_get(
                    raw,
                    "rocketmq.acl",
                    "secret_key_env",
                    default=RocketMQAclConfig.secret_key_env,
                ),
                security_token_env=_get(
                    raw,
                    "rocketmq.acl",
                    "security_token_env",
                    default=RocketMQAclConfig.security_token_env,
                ),
            ),
        ),
    )
    config = _apply_env_overrides(config)
    _validate_media_contract(config.freeswitch)
    _validate_realtime_provider_config(config.realtime)
    _validate_opening_audio_provider_config(config.opening_audio)
    _validate_postgres_config(config.postgres)
    _validate_call_recording_config(config.call_recording)
    _validate_human_transcript_config(config.human_transcript, config.features)
    _validate_handoff_config(config.handoff)
    _validate_flow_callback_config(config.flow_callback)
    _validate_doubao_tts_config(config.doubao_tts)
    _validate_qwen_omni_realtime_config(config.qwen_omni_realtime)
    _validate_qwen_opening_audio_config(config.qwen_opening_audio)
    _validate_cross_feature_config(config)
    _validate_rocketmq_config(config.rocketmq)
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
    value = _section(raw, section).get(name, default)
    return str(value)


def _get_int(
    raw: dict[str, Any],
    section: str,
    name: str,
    *,
    default: int,
) -> int:
    value = _section(raw, section).get(name, default)
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
    value = _section(raw, section).get(name, default)
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
    value = _section(raw, section).get(name, default)
    return _parse_bool(value, f"{section}.{name}")


def _section(raw: dict[str, Any], section: str) -> dict[str, Any]:
    table: Any = raw
    for part in section.split("."):
        if not isinstance(table, dict):
            return {}
        table = table.get(part, {})
    return table if isinstance(table, dict) else {}


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
        outbound=OutboundCallConfig(
            enabled=_env_bool(
                "OUTBOUND_CALLS_ENABLED",
                config.outbound.enabled,
            ),
            endpoint_template=os.getenv(
                "OUTBOUND_ENDPOINT_TEMPLATE",
                config.outbound.endpoint_template,
            ),
            dialplan_extension=os.getenv(
                "OUTBOUND_DIALPLAN_EXTENSION",
                config.outbound.dialplan_extension,
            ),
            dialplan_context=os.getenv(
                "OUTBOUND_DIALPLAN_CONTEXT",
                config.outbound.dialplan_context,
            ),
            caller_id_name=os.getenv(
                "OUTBOUND_CALLER_ID_NAME",
                config.outbound.caller_id_name,
            ),
            caller_id_number=os.getenv(
                "OUTBOUND_CALLER_ID_NUMBER",
                config.outbound.caller_id_number,
            ),
            originate_timeout_seconds=_env_int(
                "OUTBOUND_ORIGINATE_TIMEOUT_SECONDS",
                config.outbound.originate_timeout_seconds,
            ),
            max_recent_calls=_env_int(
                "OUTBOUND_MAX_RECENT_CALLS",
                config.outbound.max_recent_calls,
            ),
        ),
        realtime=RealtimeProviderConfig(
            provider=os.getenv("REALTIME_PROVIDER", config.realtime.provider),
        ),
        opening_audio=OpeningAudioProviderConfig(
            provider=os.getenv(
                "OPENING_AUDIO_PROVIDER",
                config.opening_audio.provider,
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
        doubao_tts=DoubaoTTSConfig(
            enabled=_env_bool("DOUBAO_TTS_ENABLED", config.doubao_tts.enabled),
            app_id_env=os.getenv(
                "DOUBAO_TTS_APP_ID_ENV",
                config.doubao_tts.app_id_env,
            ),
            access_token_env=os.getenv(
                "DOUBAO_TTS_ACCESS_TOKEN_ENV",
                config.doubao_tts.access_token_env,
            ),
            api_key_env=os.getenv(
                "DOUBAO_TTS_API_KEY_ENV",
                config.doubao_tts.api_key_env,
            ),
            resource_id=os.getenv(
                "DOUBAO_TTS_RESOURCE_ID",
                config.doubao_tts.resource_id,
            ),
            endpoint=os.getenv("DOUBAO_TTS_ENDPOINT", config.doubao_tts.endpoint),
            female_speaker=os.getenv(
                "DOUBAO_TTS_FEMALE_SPEAKER",
                config.doubao_tts.female_speaker,
            ),
            male_speaker=os.getenv(
                "DOUBAO_TTS_MALE_SPEAKER",
                config.doubao_tts.male_speaker,
            ),
            audio_format=os.getenv(
                "DOUBAO_TTS_AUDIO_FORMAT",
                config.doubao_tts.audio_format,
            ),
            output_sample_rate=_env_int(
                "DOUBAO_TTS_OUTPUT_SAMPLE_RATE",
                config.doubao_tts.output_sample_rate,
            ),
            timeout_seconds=_env_float(
                "DOUBAO_TTS_TIMEOUT_SECONDS",
                config.doubao_tts.timeout_seconds,
            ),
            fallback_to_s2s=_env_bool(
                "DOUBAO_TTS_FALLBACK_TO_S2S",
                config.doubao_tts.fallback_to_s2s,
            ),
        ),
        qwen_omni_realtime=QwenOmniRealtimeConfig(
            api_key_env=os.getenv(
                "QWEN_OMNI_REALTIME_API_KEY_ENV",
                config.qwen_omni_realtime.api_key_env,
            ),
            websocket_url=os.getenv(
                "QWEN_OMNI_REALTIME_WS_URL",
                config.qwen_omni_realtime.websocket_url,
            ),
            model=os.getenv(
                "QWEN_OMNI_REALTIME_MODEL",
                config.qwen_omni_realtime.model,
            ),
            voice=os.getenv(
                "QWEN_OMNI_REALTIME_VOICE",
                config.qwen_omni_realtime.voice,
            ),
            output_sample_rate=_env_int(
                "QWEN_OMNI_REALTIME_OUTPUT_SAMPLE_RATE",
                config.qwen_omni_realtime.output_sample_rate,
            ),
        ),
        qwen_opening_audio=QwenOpeningAudioConfig(
            api_key_env=os.getenv(
                "QWEN_OPENING_API_KEY_ENV",
                config.qwen_opening_audio.api_key_env,
            ),
            endpoint=os.getenv(
                "QWEN_OPENING_ENDPOINT",
                config.qwen_opening_audio.endpoint,
            ),
            model=os.getenv(
                "QWEN_OPENING_MODEL",
                config.qwen_opening_audio.model,
            ),
            voice=os.getenv(
                "QWEN_OPENING_VOICE",
                config.qwen_opening_audio.voice,
            ),
            language_type=os.getenv(
                "QWEN_OPENING_LANGUAGE_TYPE",
                config.qwen_opening_audio.language_type,
            ),
            timeout_seconds=_env_float(
                "QWEN_OPENING_TIMEOUT_SECONDS",
                config.qwen_opening_audio.timeout_seconds,
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
            recording_dir=os.getenv(
                "RECORDING_DIR",
                config.features.recording_dir,
            ),
            recording_host_dir=os.getenv(
                "RECORDING_HOST_DIR",
                config.features.recording_host_dir,
            ),
            inbound_rms_diagnostics_enabled=_env_bool(
                "INBOUND_RMS_DIAGNOSTICS_ENABLED",
                config.features.inbound_rms_diagnostics_enabled,
            ),
        ),
        call_recording=CallRecordingConfig(
            enabled=_env_bool(
                "CALL_RECORDING_ENABLED",
                config.call_recording.enabled,
            ),
            directory=os.getenv(
                "CALL_RECORDING_DIR",
                config.call_recording.directory,
            ),
            upload_enabled=_env_bool(
                "CALL_RECORDING_UPLOAD_ENABLED",
                config.call_recording.upload_enabled,
            ),
            host_directory=os.getenv(
                "CALL_RECORDING_HOST_DIR",
                config.call_recording.host_directory,
            ),
            object_prefix=os.getenv(
                "CALL_RECORDING_OBJECT_PREFIX",
                config.call_recording.object_prefix,
            ),
            upload_timeout_seconds=_env_float(
                "CALL_RECORDING_UPLOAD_TIMEOUT_SECONDS",
                config.call_recording.upload_timeout_seconds,
            ),
            opening_warmup_ms=_env_int(
                "CALL_RECORDING_OPENING_WARMUP_MS",
                config.call_recording.opening_warmup_ms,
            ),
            opening_source_debug_enabled=_env_bool(
                "CALL_RECORDING_OPENING_SOURCE_DEBUG_ENABLED",
                config.call_recording.opening_source_debug_enabled,
            ),
        ),
        postgres=PostgresConfig(
            enabled=_env_bool("POSTGRES_ENABLED", config.postgres.enabled),
            dsn_env=os.getenv("POSTGRES_DSN_ENV", config.postgres.dsn_env),
            min_pool_size=_env_int(
                "POSTGRES_MIN_POOL_SIZE",
                config.postgres.min_pool_size,
            ),
            max_pool_size=_env_int(
                "POSTGRES_MAX_POOL_SIZE",
                config.postgres.max_pool_size,
            ),
            command_timeout_seconds=_env_float(
                "POSTGRES_COMMAND_TIMEOUT_SECONDS",
                config.postgres.command_timeout_seconds,
            ),
        ),
        human_transcript=HumanTranscriptConfig(
            enabled=_env_bool(
                "HUMAN_TRANSCRIPT_ENABLED",
                config.human_transcript.enabled,
            ),
            provider=os.getenv(
                "HUMAN_TRANSCRIPT_PROVIDER",
                config.human_transcript.provider,
            ),
            http_url=os.getenv(
                "HUMAN_TRANSCRIPT_HTTP_URL",
                config.human_transcript.http_url,
            ),
            timeout_seconds=_env_float(
                "HUMAN_TRANSCRIPT_TIMEOUT_SECONDS",
                config.human_transcript.timeout_seconds,
            ),
        ),
        handoff=HandoffConfig(
            wait_timeout_seconds=_env_int(
                "HANDOFF_WAIT_TIMEOUT_SECONDS",
                config.handoff.wait_timeout_seconds,
            ),
        ),
        flow_callback=FlowCallbackConfig(
            enabled=_env_bool(
                "FLOW_CALLBACK_ENABLED",
                config.flow_callback.enabled,
            ),
            topic=os.getenv("FLOW_CALLBACK_TOPIC", config.flow_callback.topic),
            producer_group=os.getenv(
                "FLOW_CALLBACK_PRODUCER_GROUP",
                config.flow_callback.producer_group,
            ),
            http=FlowCallbackHttpConfig(
                enabled=_env_bool(
                    "FLOW_CALLBACK_HTTP_ENABLED",
                    config.flow_callback.http.enabled,
                ),
                base_url=os.getenv(
                    "FLOW_CALLBACK_HTTP_BASE_URL",
                    config.flow_callback.http.base_url,
                ),
                path=os.getenv(
                    "FLOW_CALLBACK_HTTP_PATH",
                    config.flow_callback.http.path,
                ),
                client_id=os.getenv(
                    "FLOW_CALLBACK_HTTP_CLIENT_ID",
                    config.flow_callback.http.client_id,
                ),
                secret_env=os.getenv(
                    "FLOW_CALLBACK_HTTP_SECRET_ENV",
                    config.flow_callback.http.secret_env,
                ),
                timeout_seconds=_env_float(
                    "FLOW_CALLBACK_HTTP_TIMEOUT_SECONDS",
                    config.flow_callback.http.timeout_seconds,
                ),
                max_attempts=_env_int(
                    "FLOW_CALLBACK_HTTP_MAX_ATTEMPTS",
                    config.flow_callback.http.max_attempts,
                ),
                retry_backoff_seconds=_env_float(
                    "FLOW_CALLBACK_HTTP_RETRY_BACKOFF_SECONDS",
                    config.flow_callback.http.retry_backoff_seconds,
                ),
            ),
        ),
        rocketmq=RocketMQConfig(
            enabled=_env_bool("ROCKETMQ_ENABLED", config.rocketmq.enabled),
            endpoint=os.getenv("ROCKETMQ_ENDPOINT", config.rocketmq.endpoint),
            name_server=os.getenv(
                "ROCKETMQ_NAME_SERVER",
                config.rocketmq.name_server,
            ),
            producer_group=os.getenv(
                "ROCKETMQ_PRODUCER_GROUP",
                config.rocketmq.producer_group,
            ),
            callback_topic=os.getenv(
                "ROCKETMQ_CALLBACK_TOPIC",
                config.rocketmq.callback_topic,
            ),
            send_timeout_ms=_env_int(
                "ROCKETMQ_SEND_TIMEOUT_MS",
                config.rocketmq.send_timeout_ms,
            ),
            acl=RocketMQAclConfig(
                enabled=_env_bool(
                    "ROCKETMQ_ACL_ENABLED",
                    config.rocketmq.acl.enabled,
                ),
                access_key_env=os.getenv(
                    "ROCKETMQ_ACCESS_KEY_ENV",
                    config.rocketmq.acl.access_key_env,
                ),
                secret_key_env=os.getenv(
                    "ROCKETMQ_SECRET_KEY_ENV",
                    config.rocketmq.acl.secret_key_env,
                ),
                security_token_env=os.getenv(
                    "ROCKETMQ_SECURITY_TOKEN_ENV",
                    config.rocketmq.acl.security_token_env,
                ),
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


def _validate_realtime_provider_config(config: RealtimeProviderConfig) -> None:
    if config.provider not in {"doubao_s2s", "qwen_omni_realtime"}:
        raise ValueError("realtime.provider must be doubao_s2s or qwen_omni_realtime")


def _validate_opening_audio_provider_config(
    config: OpeningAudioProviderConfig,
) -> None:
    if config.provider not in {"doubao_tts", "qwen"}:
        raise ValueError("opening_audio.provider must be doubao_tts or qwen")


def _validate_postgres_config(config: PostgresConfig) -> None:
    if config.min_pool_size < 0:
        raise ValueError("postgres.min_pool_size must be non-negative")
    if config.max_pool_size < 1:
        raise ValueError("postgres.max_pool_size must be positive")
    if config.min_pool_size > config.max_pool_size:
        raise ValueError("postgres.min_pool_size must not exceed max_pool_size")
    if config.command_timeout_seconds <= 0:
        raise ValueError("postgres.command_timeout_seconds must be positive")


def _validate_call_recording_config(config: CallRecordingConfig) -> None:
    if not config.directory.strip():
        raise ValueError("call_recording.directory is required")
    forbidden = (" ", "\t", "\n", "\r", ",", "{", "}")
    if any(char in config.directory for char in forbidden):
        raise ValueError(
            "call_recording.directory must not contain whitespace, comma, or braces"
        )
    if config.upload_enabled and not config.enabled:
        raise ValueError("call_recording.enabled must be true when upload is enabled")
    if config.upload_enabled and not config.host_directory.strip():
        raise ValueError(
            "call_recording.host_directory is required when upload is enabled"
        )
    if not config.object_prefix.strip():
        raise ValueError("call_recording.object_prefix is required")
    if any(char in config.object_prefix for char in forbidden):
        raise ValueError(
            "call_recording.object_prefix must not contain whitespace, comma, or braces"
        )
    if config.upload_timeout_seconds <= 0:
        raise ValueError("call_recording.upload_timeout_seconds must be positive")
    if config.opening_warmup_ms < 0:
        raise ValueError("call_recording.opening_warmup_ms must be non-negative")


def _validate_human_transcript_config(
    config: HumanTranscriptConfig,
    features: FeatureConfig,
) -> None:
    if config.timeout_seconds <= 0:
        raise ValueError("human_transcript.timeout_seconds must be positive")
    if config.provider not in {"http_json", "mock"}:
        raise ValueError("human_transcript.provider must be http_json or mock")
    if (
        config.enabled
        and config.provider == "http_json"
        and not config.http_url.strip()
    ):
        raise ValueError("human_transcript.http_url is required when enabled")
    if config.enabled and not features.recording_enabled:
        raise ValueError("recording must be enabled when human_transcript is enabled")


def _validate_handoff_config(config: HandoffConfig) -> None:
    if not 1 <= config.wait_timeout_seconds <= 300:
        raise ValueError("handoff.wait_timeout_seconds must be between 1 and 300")


def _validate_flow_callback_config(config: FlowCallbackConfig) -> None:
    http = config.http
    if http.timeout_seconds <= 0:
        raise ValueError("flow_callback.http.timeout_seconds must be positive")
    if http.max_attempts < 1:
        raise ValueError("flow_callback.http.max_attempts must be positive")
    if http.retry_backoff_seconds < 0:
        raise ValueError("flow_callback.http.retry_backoff_seconds must be non-negative")
    if not http.path.startswith("/"):
        raise ValueError("flow_callback.http.path must start with /")
    if http.enabled and not http.base_url.strip():
        raise ValueError("flow_callback.http.base_url is required when enabled")
    if http.enabled and not http.client_id.strip():
        raise ValueError("flow_callback.http.client_id is required when enabled")
    if http.enabled and not http.secret_env.strip():
        raise ValueError("flow_callback.http.secret_env is required when enabled")


def _validate_doubao_tts_config(config: DoubaoTTSConfig) -> None:
    if not config.resource_id.strip():
        raise ValueError("doubao_tts.resource_id is required")
    if not config.endpoint.strip():
        raise ValueError("doubao_tts.endpoint is required")
    if not config.female_speaker.strip():
        raise ValueError("doubao_tts.female_speaker is required")
    if not config.male_speaker.strip():
        raise ValueError("doubao_tts.male_speaker is required")
    if config.audio_format != "pcm":
        raise ValueError("doubao_tts.audio_format must be pcm")
    if config.output_sample_rate not in {
        8000,
        16000,
        22050,
        24000,
        32000,
        44100,
        48000,
    }:
        raise ValueError("doubao_tts.output_sample_rate is unsupported")
    if config.timeout_seconds <= 0:
        raise ValueError("doubao_tts.timeout_seconds must be positive")


def _validate_qwen_omni_realtime_config(config: QwenOmniRealtimeConfig) -> None:
    if not config.api_key_env.strip():
        raise ValueError("qwen_omni_realtime.api_key_env is required")
    if not config.websocket_url.strip():
        raise ValueError("qwen_omni_realtime.websocket_url is required")
    if not config.model.strip():
        raise ValueError("qwen_omni_realtime.model is required")
    if not config.voice.strip():
        raise ValueError("qwen_omni_realtime.voice is required")
    if config.output_sample_rate != 24000:
        raise ValueError("qwen_omni_realtime.output_sample_rate must be 24000")


def _validate_qwen_opening_audio_config(config: QwenOpeningAudioConfig) -> None:
    if not config.api_key_env.strip():
        raise ValueError("qwen_opening_audio.api_key_env is required")
    if not config.endpoint.strip():
        raise ValueError("qwen_opening_audio.endpoint is required")
    if not config.model.strip():
        raise ValueError("qwen_opening_audio.model is required")
    if not config.voice.strip():
        raise ValueError("qwen_opening_audio.voice is required")
    if not config.language_type.strip():
        raise ValueError("qwen_opening_audio.language_type is required")
    if config.timeout_seconds <= 0:
        raise ValueError("qwen_opening_audio.timeout_seconds must be positive")


def _validate_cross_feature_config(config: GatewayConfig) -> None:
    if config.flow_callback.enabled and not config.postgres.enabled:
        raise ValueError("postgres must be enabled when flow_callback is enabled")
    if config.call_recording.upload_enabled and not config.postgres.enabled:
        raise ValueError(
            "postgres.enabled must be true when call_recording upload is enabled"
        )


def _validate_rocketmq_config(config: RocketMQConfig) -> None:
    if config.send_timeout_ms <= 0:
        raise ValueError("rocketmq.send_timeout_ms must be positive")
    if config.enabled:
        if not config.name_server.strip():
            raise ValueError("rocketmq.name_server is required when enabled")
        if not config.callback_topic.strip():
            raise ValueError("rocketmq.callback_topic is required when enabled")
        if not config.producer_group.strip():
            raise ValueError("rocketmq.producer_group is required when enabled")
