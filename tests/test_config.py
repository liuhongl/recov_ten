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
            recording_enabled = true
            recording_dir = "/tmp/recov_ten_handoff_test"
            recording_host_dir = "./freeswitch-local/recordings/handoff"
            inbound_rms_diagnostics_enabled = true

            [call_recording]
            enabled = true
            directory = "/var/lib/freeswitch/recordings"
            upload_enabled = true
            host_directory = "./freeswitch-local/recordings"
            object_prefix = "recordings"
            upload_timeout_seconds = 12.5
            opening_warmup_ms = 700
            opening_source_debug_enabled = true

            [human_transcript]
            enabled = true
            provider = "http_json"
            http_url = "http://asr.example/transcribe"
            timeout_seconds = 12.5

            [handoff]
            wait_timeout_seconds = 15

            [flow_callback]
            enabled = true
            topic = "recov-flow-callback"
            producer_group = "recov-ten-gateway"

            [flow_callback.http]
            enabled = true
            base_url = "https://flow.example"
            path = "/system/recov/flow/external/callback"
            client_id = "python-ai-call"
            secret_env = "TEST_FLOW_CALLBACK_SECRET"
            timeout_seconds = 8.5
            max_attempts = 3
            retry_backoff_seconds = 0.25

            [rocketmq]
            enabled = true
            endpoint = "http://118.89.137.44/"
            name_server = "118.89.137.44:9876"
            producer_group = "recov-ten-gateway"
            callback_topic = "recov-flow-callback"
            send_timeout_ms = 4500

            [rocketmq.acl]
            enabled = true
            access_key_env = "TEST_ROCKETMQ_ACCESS_KEY"
            secret_key_env = "TEST_ROCKETMQ_SECRET_KEY"
            security_token_env = "TEST_ROCKETMQ_SECURITY_TOKEN"

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
    assert config.features.recording_enabled is True
    assert config.features.recording_dir == "/tmp/recov_ten_handoff_test"
    assert config.features.recording_host_dir == "./freeswitch-local/recordings/handoff"
    assert config.features.inbound_rms_diagnostics_enabled is True
    assert config.call_recording.enabled is True
    assert config.call_recording.directory == "/var/lib/freeswitch/recordings"
    assert config.call_recording.upload_enabled is True
    assert config.call_recording.host_directory == "./freeswitch-local/recordings"
    assert config.call_recording.object_prefix == "recordings"
    assert config.call_recording.upload_timeout_seconds == 12.5
    assert config.call_recording.opening_warmup_ms == 700
    assert config.call_recording.opening_source_debug_enabled is True
    assert config.human_transcript.enabled is True
    assert config.human_transcript.provider == "http_json"
    assert config.human_transcript.http_url == "http://asr.example/transcribe"
    assert config.human_transcript.timeout_seconds == 12.5
    assert config.handoff.wait_timeout_seconds == 15
    assert config.flow_callback.enabled is True
    assert config.flow_callback.topic == "recov-flow-callback"
    assert config.flow_callback.producer_group == "recov-ten-gateway"
    assert config.flow_callback.http.enabled is True
    assert config.flow_callback.http.base_url == "https://flow.example"
    assert config.flow_callback.http.path == "/system/recov/flow/external/callback"
    assert config.flow_callback.http.client_id == "python-ai-call"
    assert config.flow_callback.http.secret_env == "TEST_FLOW_CALLBACK_SECRET"
    assert config.flow_callback.http.timeout_seconds == 8.5
    assert config.flow_callback.http.max_attempts == 3
    assert config.flow_callback.http.retry_backoff_seconds == 0.25
    assert config.rocketmq.enabled is True
    assert config.rocketmq.endpoint == "http://118.89.137.44/"
    assert config.rocketmq.name_server == "118.89.137.44:9876"
    assert config.rocketmq.producer_group == "recov-ten-gateway"
    assert config.rocketmq.callback_topic == "recov-flow-callback"
    assert config.rocketmq.send_timeout_ms == 4500
    assert config.rocketmq.acl.enabled is True
    assert config.rocketmq.acl.access_key_env == "TEST_ROCKETMQ_ACCESS_KEY"
    assert config.rocketmq.acl.secret_key_env == "TEST_ROCKETMQ_SECRET_KEY"
    assert config.rocketmq.acl.security_token_env == "TEST_ROCKETMQ_SECURITY_TOKEN"
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
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    monkeypatch.setenv("RECORDING_DIR", "/tmp/env-recordings")
    monkeypatch.setenv("CALL_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CALL_RECORDING_DIR", "/var/lib/env-recordings")
    monkeypatch.setenv("CALL_RECORDING_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("CALL_RECORDING_HOST_DIR", "/opt/recov_ten/recordings")
    monkeypatch.setenv("CALL_RECORDING_OBJECT_PREFIX", "recordings")
    monkeypatch.setenv("CALL_RECORDING_UPLOAD_TIMEOUT_SECONDS", "9.5")
    monkeypatch.setenv("CALL_RECORDING_OPENING_WARMUP_MS", "800")
    monkeypatch.setenv("CALL_RECORDING_OPENING_SOURCE_DEBUG_ENABLED", "true")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_ENABLED", "true")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_PROVIDER", "http_json")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_HTTP_URL", "http://env-asr.example/transcribe")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_TIMEOUT_SECONDS", "13.5")
    monkeypatch.setenv("HANDOFF_WAIT_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("RECORDING_HOST_DIR", "/opt/recov_ten/handoff-recordings")
    monkeypatch.setenv("FLOW_CALLBACK_ENABLED", "true")
    monkeypatch.setenv("FLOW_CALLBACK_TOPIC", "env-flow-callback")
    monkeypatch.setenv("FLOW_CALLBACK_PRODUCER_GROUP", "env-producer")
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_ENABLED", "true")
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_BASE_URL", "https://env-flow.example")
    monkeypatch.setenv(
        "FLOW_CALLBACK_HTTP_PATH",
        "/system/recov/flow/external/callback",
    )
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_CLIENT_ID", "env-python-ai-call")
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_SECRET_ENV", "ENV_FLOW_CALLBACK_SECRET")
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_TIMEOUT_SECONDS", "9.5")
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("FLOW_CALLBACK_HTTP_RETRY_BACKOFF_SECONDS", "0.75")
    monkeypatch.setenv("ROCKETMQ_ENABLED", "true")
    monkeypatch.setenv("ROCKETMQ_ENDPOINT", "http://mq.example/")
    monkeypatch.setenv("ROCKETMQ_NAME_SERVER", "mq.example:9876")
    monkeypatch.setenv("ROCKETMQ_PRODUCER_GROUP", "env-mq-producer")
    monkeypatch.setenv("ROCKETMQ_CALLBACK_TOPIC", "env-flow-callback")
    monkeypatch.setenv("ROCKETMQ_SEND_TIMEOUT_MS", "4500")
    monkeypatch.setenv("ROCKETMQ_ACL_ENABLED", "true")
    monkeypatch.setenv("ROCKETMQ_ACCESS_KEY_ENV", "ENV_ROCKETMQ_ACCESS_KEY")
    monkeypatch.setenv("ROCKETMQ_SECRET_KEY_ENV", "ENV_ROCKETMQ_SECRET_KEY")
    monkeypatch.setenv("ROCKETMQ_SECURITY_TOKEN_ENV", "ENV_ROCKETMQ_TOKEN")
    monkeypatch.setenv("POSTGRES_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_DSN_ENV", "LOCAL_POSTGRES_DSN")
    monkeypatch.setenv("POSTGRES_MAX_POOL_SIZE", "9")
    monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("INBOUND_RMS_DIAGNOSTICS_ENABLED", "true")

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
    assert config.features.recording_enabled is True
    assert config.features.recording_dir == "/tmp/env-recordings"
    assert config.features.recording_host_dir == "/opt/recov_ten/handoff-recordings"
    assert config.features.inbound_rms_diagnostics_enabled is True
    assert config.call_recording.enabled is True
    assert config.call_recording.directory == "/var/lib/env-recordings"
    assert config.call_recording.upload_enabled is True
    assert config.call_recording.host_directory == "/opt/recov_ten/recordings"
    assert config.call_recording.object_prefix == "recordings"
    assert config.call_recording.upload_timeout_seconds == 9.5
    assert config.call_recording.opening_warmup_ms == 800
    assert config.call_recording.opening_source_debug_enabled is True
    assert config.human_transcript.enabled is True
    assert config.human_transcript.provider == "http_json"
    assert config.human_transcript.http_url == "http://env-asr.example/transcribe"
    assert config.human_transcript.timeout_seconds == 13.5
    assert config.handoff.wait_timeout_seconds == 12
    assert config.flow_callback.enabled is True
    assert config.flow_callback.topic == "env-flow-callback"
    assert config.flow_callback.producer_group == "env-producer"
    assert config.flow_callback.http.enabled is True
    assert config.flow_callback.http.base_url == "https://env-flow.example"
    assert config.flow_callback.http.path == "/system/recov/flow/external/callback"
    assert config.flow_callback.http.client_id == "env-python-ai-call"
    assert config.flow_callback.http.secret_env == "ENV_FLOW_CALLBACK_SECRET"
    assert config.flow_callback.http.timeout_seconds == 9.5
    assert config.flow_callback.http.max_attempts == 4
    assert config.flow_callback.http.retry_backoff_seconds == 0.75
    assert config.rocketmq.enabled is True
    assert config.rocketmq.endpoint == "http://mq.example/"
    assert config.rocketmq.name_server == "mq.example:9876"
    assert config.rocketmq.producer_group == "env-mq-producer"
    assert config.rocketmq.callback_topic == "env-flow-callback"
    assert config.rocketmq.send_timeout_ms == 4500
    assert config.rocketmq.acl.enabled is True
    assert config.rocketmq.acl.access_key_env == "ENV_ROCKETMQ_ACCESS_KEY"
    assert config.rocketmq.acl.secret_key_env == "ENV_ROCKETMQ_SECRET_KEY"
    assert config.rocketmq.acl.security_token_env == "ENV_ROCKETMQ_TOKEN"
    assert config.postgres.enabled is True
    assert config.postgres.dsn_env == "LOCAL_POSTGRES_DSN"
    assert config.postgres.max_pool_size == 9
    assert config.postgres.command_timeout_seconds == 2.5


def test_human_transcript_requires_recording_enabled(monkeypatch):
    monkeypatch.setenv("HUMAN_TRANSCRIPT_ENABLED", "true")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_HTTP_URL", "http://asr.example/transcribe")
    monkeypatch.setenv("RECORDING_ENABLED", "false")

    with pytest.raises(ValueError) as exc_info:
        load_config()

    assert "recording must be enabled when human_transcript is enabled" in str(
        exc_info.value
    )


def test_call_recording_upload_requires_postgres_enabled(monkeypatch):
    monkeypatch.setenv("CALL_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CALL_RECORDING_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("CALL_RECORDING_HOST_DIR", "/opt/recov_ten/recordings")
    monkeypatch.setenv("POSTGRES_ENABLED", "false")

    with pytest.raises(ValueError) as exc_info:
        load_config()

    assert "postgres.enabled must be true when call_recording upload is enabled" in str(
        exc_info.value
    )


def test_call_recording_upload_timeout_must_be_positive(monkeypatch):
    monkeypatch.setenv("CALL_RECORDING_UPLOAD_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError) as exc_info:
        load_config()

    assert "call_recording.upload_timeout_seconds must be positive" in str(
        exc_info.value
    )


def test_human_transcript_allows_mock_provider(monkeypatch):
    monkeypatch.setenv("HUMAN_TRANSCRIPT_ENABLED", "true")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_PROVIDER", "mock")
    monkeypatch.setenv("HUMAN_TRANSCRIPT_HTTP_URL", "")
    monkeypatch.setenv("RECORDING_ENABLED", "true")

    config = load_config()

    assert config.human_transcript.enabled is True
    assert config.human_transcript.provider == "mock"
    assert config.human_transcript.http_url == ""


def test_flow_callback_requires_postgres_enabled(monkeypatch):
    monkeypatch.setenv("FLOW_CALLBACK_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_ENABLED", "false")

    with pytest.raises(ValueError) as exc_info:
        load_config()

    assert "postgres must be enabled when flow_callback is enabled" in str(
        exc_info.value
    )


def test_default_outbound_caller_avoids_local_self_call():
    config = load_config()

    assert config.outbound.caller_id_number == "9000"
    assert config.features.inbound_rms_diagnostics_enabled is False


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


def test_rejects_invalid_flow_callback_http_path(tmp_path):
    config_file = tmp_path / "bad.toml"
    config_file.write_text(
        textwrap.dedent(
            """
            [flow_callback.http]
            enabled = true
            base_url = "https://flow.example"
            path = "system/recov/flow/external/callback"
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="flow_callback.http.path"):
        load_config(config_file)
