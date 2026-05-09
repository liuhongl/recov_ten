from __future__ import annotations

from app.audio_codec import samples_to_pcm_s16le
from app.config import VadConfig
from app.voice_activity import EnergyVadTurnDetector


def test_energy_vad_detects_turn_after_trailing_silence():
    detector = EnergyVadTurnDetector(
        VadConfig(
            speech_rms_threshold=300,
            start_speech_ms=20,
            end_silence_ms=40,
            min_speech_ms=20,
            max_utterance_ms=1000,
            pre_speech_ms=0,
            keep_silence_ms=0,
        ),
        frame_bytes=320,
        frame_duration_ms=20,
    )
    speech = _frame(1200)
    silence = _frame(0)

    assert detector.process_frame(speech) is None
    assert detector.process_frame(silence) is None
    turn = detector.process_frame(silence)

    assert turn is not None
    assert turn.reason == "silence"
    assert turn.pcm == speech
    assert turn.duration_ms == 20
    assert turn.speech_ms == 20


def test_energy_vad_ignores_short_noise():
    detector = EnergyVadTurnDetector(
        VadConfig(
            speech_rms_threshold=300,
            start_speech_ms=40,
            end_silence_ms=40,
            min_speech_ms=40,
            max_utterance_ms=1000,
            pre_speech_ms=0,
            keep_silence_ms=0,
        ),
        frame_bytes=320,
        frame_duration_ms=20,
    )

    assert detector.process_frame(_frame(1200)) is None
    assert detector.process_frame(_frame(0)) is None
    assert detector.finish_if_active() is None


def _frame(value: int) -> bytes:
    return samples_to_pcm_s16le([value] * 160)
