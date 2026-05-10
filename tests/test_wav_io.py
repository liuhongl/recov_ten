from __future__ import annotations

import wave

from app.audio_codec import pcm_s16le_sample_count, samples_to_pcm_s16le
from app.wav_io import read_wav_as_pcm16_mono, write_pcm16_wav


def test_write_pcm16_wav(tmp_path):
    output = tmp_path / "audio.wav"
    write_pcm16_wav(output, b"\x00\x00\x01\x00", sample_rate=24000)

    with wave.open(str(output), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.readframes(2) == b"\x00\x00\x01\x00"


def test_read_wav_as_pcm16_mono_resamples(tmp_path):
    source = tmp_path / "source.wav"
    pcm = samples_to_pcm_s16le(range(240))
    write_pcm16_wav(source, pcm, sample_rate=24000)

    actual_pcm, sample_rate = read_wav_as_pcm16_mono(
        source,
        target_sample_rate=16000,
    )

    assert sample_rate == 16000
    assert pcm_s16le_sample_count(actual_pcm) == 160


def test_read_wav_as_pcm16_mono_downmixes_stereo(tmp_path):
    source = tmp_path / "stereo.wav"
    with wave.open(str(source), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(samples_to_pcm_s16le([100, 300, -100, -300]))

    actual_pcm, sample_rate = read_wav_as_pcm16_mono(source)

    assert sample_rate == 8000
    assert actual_pcm == samples_to_pcm_s16le([200, -200])
