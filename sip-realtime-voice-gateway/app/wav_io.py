from __future__ import annotations

import wave
from pathlib import Path

from .audio_codec import (
    pcm_s16le_to_samples,
    resample_pcm_s16le_mono,
    samples_to_pcm_s16le,
)


def read_wav_as_pcm16_mono(
    path: str | Path,
    *,
    target_sample_rate: int | None = None,
) -> tuple[bytes, int]:
    input_path = Path(path)
    with wave.open(str(input_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        compression = wav_file.getcomptype()
        pcm = wav_file.readframes(wav_file.getnframes())

    if compression != "NONE":
        raise ValueError("only uncompressed PCM WAV is supported")
    if sample_width != 2:
        raise ValueError("only 16-bit PCM WAV is supported")
    if channels < 1:
        raise ValueError("WAV must contain at least one channel")

    if channels > 1:
        pcm = _downmix_pcm16_to_mono(pcm, channels)

    if target_sample_rate is not None and sample_rate != target_sample_rate:
        pcm = resample_pcm_s16le_mono(pcm, sample_rate, target_sample_rate)
        sample_rate = target_sample_rate

    return pcm, sample_rate


def write_pcm16_wav(
    path: str | Path,
    pcm: bytes,
    *,
    sample_rate: int,
    channels: int = 1,
) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)


def _downmix_pcm16_to_mono(pcm: bytes, channels: int) -> bytes:
    samples = pcm_s16le_to_samples(pcm)
    if len(samples) % channels != 0:
        raise ValueError("interleaved PCM sample count is not divisible by channels")

    mono_samples = []
    for offset in range(0, len(samples), channels):
        frame = samples[offset : offset + channels]
        mono_samples.append(round(sum(frame) / channels))
    return samples_to_pcm_s16le(mono_samples)
