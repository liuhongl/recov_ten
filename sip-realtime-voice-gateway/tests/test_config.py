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
            echo_mode = "resample_16k_roundtrip"

            [realtime]
            provider = "aliyun"
            url = "wss://example.test/realtime"
            model = "qwen3.5-omni-plus-realtime"
            voice = "Cherry"

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
            """
        ),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9999
    assert config.logging.level == "DEBUG"
    assert config.freeswitch.sample_rate == 8000
    assert config.freeswitch.echo_mode == "resample_16k_roundtrip"
    assert config.realtime.model == "qwen3.5-omni-plus-realtime"
    assert config.vad.speech_rms_threshold == 400
    assert config.vad.end_silence_ms == 700
    assert config.vad.barge_in_enabled is True
    assert config.features.metrics_enabled is True
    assert config.features.recording_enabled is False


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("GATEWAY_PORT", "9200")
    monkeypatch.setenv("FREESWITCH_SAMPLE_RATE", "16000")
    monkeypatch.setenv("FREESWITCH_ECHO_MODE", "resample_16k_roundtrip")
    monkeypatch.setenv("ALIYUN_REALTIME_MODEL", "test-model")
    monkeypatch.setenv("VAD_END_SILENCE_MS", "600")
    monkeypatch.setenv("VAD_BARGE_IN_ENABLED", "true")
    monkeypatch.setenv("METRICS_ENABLED", "false")

    config = load_config()

    assert config.server.port == 9200
    assert config.freeswitch.sample_rate == 16000
    assert config.freeswitch.echo_mode == "resample_16k_roundtrip"
    assert config.realtime.model == "test-model"
    assert config.vad.end_silence_ms == 600
    assert config.vad.barge_in_enabled is True
    assert config.features.metrics_enabled is False


def test_invalid_boolean_env(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "maybe")

    with pytest.raises(ValueError, match="METRICS_ENABLED"):
        load_config()
