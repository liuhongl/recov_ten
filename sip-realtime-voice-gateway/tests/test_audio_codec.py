from __future__ import annotations

import math
import struct

import pytest

from app.audio_codec import (
    float32le_to_pcm_s16le,
    pcma_to_pcm_s16le,
    pcm_s16le_frame_bytes,
    pcm_s16le_rms,
    pcm_s16le_sample_count,
    pcm_s16le_to_pcma,
    pcm_s16le_to_samples,
    resample_pcm_s16le_mono,
    samples_to_pcm_s16le,
    split_audio_frames,
)


def test_pcm_frame_size_for_telephone_audio():
    assert pcm_s16le_frame_bytes(8000) == 320
    assert pcm_s16le_frame_bytes(16000) == 640
    assert pcm_s16le_frame_bytes(24000) == 960


def test_pcm_rms_reports_signal_energy():
    assert pcm_s16le_rms(samples_to_pcm_s16le([0, 0, 0])) == 0
    assert pcm_s16le_rms(samples_to_pcm_s16le([1000, -1000])) == 1000


def test_split_audio_frames_requires_aligned_audio_unless_padding():
    audio = bytes(range(10))

    with pytest.raises(ValueError, match="not aligned"):
        split_audio_frames(audio, 4)

    assert split_audio_frames(audio, 4, pad_last=True) == [
        bytes([0, 1, 2, 3]),
        bytes([4, 5, 6, 7]),
        bytes([8, 9, 0, 0]),
    ]


def test_resample_8k_to_16k_preserves_duration_and_signal():
    source = _sine_pcm(sample_rate=8000, duration_ms=1000, frequency_hz=440)

    actual = resample_pcm_s16le_mono(source, 8000, 16000)

    assert pcm_s16le_sample_count(source) == 8000
    assert pcm_s16le_sample_count(actual) == 16000
    assert _peak_abs(actual) > 10000


def test_resample_24k_to_8k_preserves_duration_and_signal():
    source = _sine_pcm(sample_rate=24000, duration_ms=1000, frequency_hz=440)

    actual = resample_pcm_s16le_mono(source, 24000, 8000)

    assert pcm_s16le_sample_count(source) == 24000
    assert pcm_s16le_sample_count(actual) == 8000
    assert _peak_abs(actual) > 10000


def test_pcma_known_values_match_g711_alaw():
    samples = [-32768, -30000, -10000, -1000, -1, 0, 1, 1000, 10000, 30000, 32767]
    expected_pcma = bytes(
        [0x2A, 0x28, 0x36, 0x7A, 0x55, 0xD5, 0xD5, 0xFA, 0xB6, 0xA8, 0xAA]
    )
    expected_decoded = [
        -32256,
        -30208,
        -9984,
        -1008,
        -8,
        8,
        8,
        1008,
        9984,
        30208,
        32256,
    ]

    encoded = pcm_s16le_to_pcma(samples_to_pcm_s16le(samples))
    decoded = pcm_s16le_to_samples(pcma_to_pcm_s16le(encoded))

    assert encoded == expected_pcma
    assert decoded == expected_decoded


def test_pcma_roundtrip_preserves_frame_shape():
    source = _sine_pcm(sample_rate=8000, duration_ms=20, frequency_hz=440)

    encoded = pcm_s16le_to_pcma(source)
    decoded = pcma_to_pcm_s16le(encoded)

    assert len(source) == 320
    assert len(encoded) == 160
    assert len(decoded) == 320
    assert _peak_abs(decoded) > 10000


def test_float32le_to_pcm_s16le_converts_normalized_samples():
    pcm = float32le_to_pcm_s16le(struct.pack("<ffff", -1.0, -0.5, 0.5, 1.0))

    assert pcm_s16le_to_samples(pcm) == [-32768, -16384, 16384, 32767]


def _sine_pcm(sample_rate: int, duration_ms: int, frequency_hz: int) -> bytes:
    sample_count = sample_rate * duration_ms // 1000
    samples = [
        round(16000 * math.sin(2 * math.pi * frequency_hz * index / sample_rate))
        for index in range(sample_count)
    ]
    return samples_to_pcm_s16le(samples)


def _peak_abs(pcm: bytes) -> int:
    return max(abs(sample) for sample in pcm_s16le_to_samples(pcm))
