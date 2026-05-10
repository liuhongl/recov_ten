from __future__ import annotations

import textwrap

import pytest

from app.config import load_config


def test_load_config_from_toml(tmp_path):
    config_file = tmp_path / "local.toml"
    config_file.write_text(
        textwrap.dedent(
            """
            [server]
            host = "0.0.0.0"
            port = 9999

            [logging]
            level = "DEBUG"

            [freeswitch]
            media_host = "127.0.0.1"
            media_port = 9101
            sample_rate = 8000
            phone_codec = "PCMA"
            channels = 1
            frame_duration_ms = 20
            echo_mode = "resample_16k_roundtrip"

            [event_socket]
            enabled = true
            host = "127.0.0.1"
            port = 18021
            password_env = "TEST_ESL_PASSWORD"

            [outbound]
            enabled = true
            endpoint_template = "sofia_contact:*/{destination}"
            dialplan_extension = "9199"
            dialplan_context = "default"
            caller_id_name = "AI_Agent"
            caller_id_number = "95500"
            originate_timeout_seconds = 45
            max_recent_calls = 300

            [doubao_s2s]
            app_id_env = "TEST_DOUBAO_APP_ID"
            access_token_env = "TEST_DOUBAO_ACCESS_TOKEN"
            app_key_env = "TEST_DOUBAO_APP_KEY"
            resource_id = "volc.speech.dialog"
            websocket_url = "wss://example.test/doubao"
            speaker = "zh_female_vv_jupiter_bigtts"
            output_sample_rate = 24000

            [server_vad]
            type = "server_vad"
            threshold = 0.7
            prefix_padding_ms = 400
            silence_duration_ms = 1200
            create_response = true
            interrupt_response = false

            [playback]
            jitter_buffer_ms = 320
            send_interval_ms = 10
            tail_silence_ms = 280

            [vad]
            speech_rms_threshold = 400
            start_speech_ms = 80
            end_silence_ms = 700
            min_speech_ms = 200
            max_utterance_ms = 9000
            pre_speech_ms = 120
            keep_silence_ms = 100
            barge_in_enabled = true

            [features]
            metrics_enabled = true
            recording_enabled = false

            [postgres]
            enabled = true
            dsn_env = "TEST_POSTGRES_DSN"
            min_pool_size = 1
            max_pool_size = 7
            command_timeout_seconds = 3.5
            """
        ),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9999
    assert config.logging.level == "DEBUG"
    assert config.freeswitch.sample_rate == 8000
    assert config.freeswitch.channels == 1
    assert config.freeswitch.frame_duration_ms == 20
    assert config.freeswitch.echo_mode == "resample_16k_roundtrip"
    assert config.event_socket.enabled is True
    assert config.event_socket.host == "127.0.0.1"
    assert config.event_socket.port == 18021
    assert config.event_socket.password_env == "TEST_ESL_PASSWORD"
    assert config.outbound.enabled is True
    assert config.outbound.endpoint_template == "sofia_contact:*/{destination}"
    assert config.outbound.caller_id_name == "AI_Agent"
    assert config.outbound.caller_id_number == "95500"
    assert config.outbound.originate_timeout_seconds == 45
    assert config.outbound.max_recent_calls == 300
    assert config.doubao_s2s.app_id_env == "TEST_DOUBAO_APP_ID"
    assert config.doubao_s2s.access_token_env == "TEST_DOUBAO_ACCESS_TOKEN"
    assert config.doubao_s2s.app_key_env == "TEST_DOUBAO_APP_KEY"
    assert config.doubao_s2s.resource_id == "volc.speech.dialog"
    assert config.doubao_s2s.websocket_url == "wss://example.test/doubao"
    assert config.doubao_s2s.speaker == "zh_female_vv_jupiter_bigtts"
    assert config.doubao_s2s.output_sample_rate == 24000
    assert config.server_vad.threshold == 0.7
    assert config.server_vad.prefix_padding_ms == 400
    assert config.server_vad.silence_duration_ms == 1200
    assert config.server_vad.interrupt_response is False
    assert config.playback.jitter_buffer_ms == 320
    assert config.playback.send_interval_ms == 10
    assert config.playback.tail_silence_ms == 280
    assert config.vad.speech_rms_threshold == 400
    assert config.vad.end_silence_ms == 700
    assert config.vad.barge_in_enabled is True
    assert config.features.metrics_enabled is True
    assert config.features.recording_enabled is False
    assert config.postgres.enabled is True
    assert config.postgres.dsn_env == "TEST_POSTGRES_DSN"
    assert config.postgres.max_pool_size == 7
    assert config.postgres.command_timeout_seconds == 3.5


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("GATEWAY_PORT", "9200")
    monkeypatch.setenv("FREESWITCH_SAMPLE_RATE", "8000")
    monkeypatch.setenv("FREESWITCH_CHANNELS", "1")
    monkeypatch.setenv("FREESWITCH_FRAME_DURATION_MS", "20")
    monkeypatch.setenv("FREESWITCH_ECHO_MODE", "resample_16k_roundtrip")
    monkeypatch.setenv("FREESWITCH_ESL_ENABLED", "true")
    monkeypatch.setenv("FREESWITCH_ESL_HOST", "127.0.0.2")
    monkeypatch.setenv("FREESWITCH_ESL_PORT", "19021")
    monkeypatch.setenv("FREESWITCH_ESL_PASSWORD_ENV", "LOCAL_ESL_PASSWORD")
    monkeypatch.setenv("OUTBOUND_ENDPOINT_TEMPLATE", "sofia/gateway/demo/{destination}")
    monkeypatch.setenv("OUTBOUND_DIALPLAN_EXTENSION", "9199")
    monkeypatch.setenv("OUTBOUND_CALLER_ID_NUMBER", "9000")
    monkeypatch.setenv("OUTBOUND_ORIGINATE_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("DOUBAO_S2S_SPEAKER", "env-speaker")
    monkeypatch.setenv("DOUBAO_S2S_WS_URL", "wss://env.example.test/doubao")
    monkeypatch.setenv("DOUBAO_S2S_OUTPUT_SAMPLE_RATE", "24000")
    monkeypatch.setenv("SERVER_VAD_THRESHOLD", "0.6")
    monkeypatch.setenv("SERVER_VAD_SILENCE_DURATION_MS", "2000")
    monkeypatch.setenv("SERVER_VAD_INTERRUPT_RESPONSE", "false")
    monkeypatch.setenv("PLAYBACK_JITTER_BUFFER_MS", "400")
    monkeypatch.setenv("PLAYBACK_SEND_INTERVAL_MS", "10")
    monkeypatch.setenv("PLAYOUT_TAIL_SILENCE_MS", "360")
    monkeypatch.setenv("VAD_END_SILENCE_MS", "600")
    monkeypatch.setenv("VAD_BARGE_IN_ENABLED", "true")
    monkeypatch.setenv("METRICS_ENABLED", "false")
    monkeypatch.setenv("POSTGRES_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_DSN_ENV", "LOCAL_POSTGRES_DSN")
    monkeypatch.setenv("POSTGRES_MAX_POOL_SIZE", "9")
    monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT_SECONDS", "2.5")

    config = load_config()

    assert config.server.port == 9200
    assert config.freeswitch.sample_rate == 8000
    assert config.freeswitch.channels == 1
    assert config.freeswitch.frame_duration_ms == 20
    assert config.freeswitch.echo_mode == "resample_16k_roundtrip"
    assert config.event_socket.enabled is True
    assert config.event_socket.host == "127.0.0.2"
    assert config.event_socket.port == 19021
    assert config.event_socket.password_env == "LOCAL_ESL_PASSWORD"
    assert config.outbound.endpoint_template == "sofia/gateway/demo/{destination}"
    assert config.outbound.dialplan_extension == "9199"
    assert config.outbound.caller_id_number == "9000"
    assert config.outbound.originate_timeout_seconds == 15
    assert config.doubao_s2s.speaker == "env-speaker"
    assert config.doubao_s2s.websocket_url == "wss://env.example.test/doubao"
    assert config.doubao_s2s.output_sample_rate == 24000
    assert config.server_vad.threshold == 0.6
    assert config.server_vad.silence_duration_ms == 2000
    assert config.server_vad.interrupt_response is False
    assert config.playback.jitter_buffer_ms == 400
    assert config.playback.send_interval_ms == 10
    assert config.playback.tail_silence_ms == 360
    assert config.vad.end_silence_ms == 600
    assert config.vad.barge_in_enabled is True
    assert config.features.metrics_enabled is False
    assert config.postgres.enabled is True
    assert config.postgres.dsn_env == "LOCAL_POSTGRES_DSN"
    assert config.postgres.max_pool_size == 9
    assert config.postgres.command_timeout_seconds == 2.5


def test_default_outbound_caller_avoids_local_self_call():
    config = load_config()

    assert config.outbound.caller_id_number == "9000"


def test_invalid_boolean_env(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "maybe")

    with pytest.raises(ValueError, match="METRICS_ENABLED"):
        load_config()


def test_rejects_non_pcma_media_contract(tmp_path):
    config_file = tmp_path / "bad.toml"
    config_file.write_text(
        textwrap.dedent(
            """
            [freeswitch]
            sample_rate = 16000
            phone_codec = "PCMA"
            channels = 1
            frame_duration_ms = 20
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sample_rate=8000"):
        load_config(config_file)
