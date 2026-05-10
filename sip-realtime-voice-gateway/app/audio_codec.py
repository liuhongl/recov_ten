from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Sequence

INT16_MIN = -32768
INT16_MAX = 32767
SAMPLE_WIDTH_BYTES = 2
DEFAULT_FRAME_DURATION_MS = 20

_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def pcm_s16le_frame_bytes(
    sample_rate: int,
    frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS,
    channels: int = 1,
) -> int:
    _validate_positive_int(sample_rate, "sample_rate")
    _validate_positive_int(frame_duration_ms, "frame_duration_ms")
    _validate_positive_int(channels, "channels")
    samples = sample_rate * frame_duration_ms // 1000
    if samples * 1000 != sample_rate * frame_duration_ms:
        raise ValueError("frame duration must map to whole samples")
    return samples * channels * SAMPLE_WIDTH_BYTES


def pcm_s16le_sample_count(pcm: bytes) -> int:
    _validate_pcm_s16le(pcm)
    return len(pcm) // SAMPLE_WIDTH_BYTES


def pcm_s16le_to_samples(pcm: bytes) -> list[int]:
    _validate_pcm_s16le(pcm)
    if not pcm:
        return []
    return list(struct.unpack(f"<{len(pcm) // SAMPLE_WIDTH_BYTES}h", pcm))


def pcm_s16le_rms(pcm: bytes) -> int:
    samples = pcm_s16le_to_samples(pcm)
    if not samples:
        return 0
    square_sum = sum(sample * sample for sample in samples)
    return round(math.sqrt(square_sum / len(samples)))


def samples_to_pcm_s16le(samples: Iterable[int | float]) -> bytes:
    return b"".join(
        struct.pack("<h", _clamp_int16(round(sample))) for sample in samples
    )


def float32le_to_pcm_s16le(pcm_float32: bytes) -> bytes:
    if len(pcm_float32) % 4 != 0:
        raise ValueError("float32 PCM byte length must be divisible by 4")
    if not pcm_float32:
        return b""

    samples = struct.unpack(f"<{len(pcm_float32) // 4}f", pcm_float32)
    return samples_to_pcm_s16le(_float_sample_to_int16(sample) for sample in samples)


def resample_pcm_s16le_mono(
    pcm: bytes,
    source_rate: int,
    target_rate: int,
) -> bytes:
    samples = pcm_s16le_to_samples(pcm)
    return samples_to_pcm_s16le(
        resample_samples_mono(samples, source_rate, target_rate)
    )


def resample_samples_mono(
    samples: Sequence[int],
    source_rate: int,
    target_rate: int,
) -> list[int]:
    _validate_positive_int(source_rate, "source_rate")
    _validate_positive_int(target_rate, "target_rate")

    if source_rate == target_rate:
        return [_clamp_int16(sample) for sample in samples]
    if not samples:
        return []

    output_count = round(len(samples) * target_rate / source_rate)
    if output_count <= 0:
        return []
    if len(samples) == 1:
        return [_clamp_int16(samples[0])] * output_count

    if target_rate < source_rate:
        return _downsample_average(samples, source_rate, target_rate, output_count)
    return _upsample_linear(samples, source_rate, target_rate, output_count)


def split_audio_frames(
    audio: bytes,
    frame_bytes: int,
    *,
    pad_last: bool = False,
) -> list[bytes]:
    _validate_positive_int(frame_bytes, "frame_bytes")
    frames: list[bytes] = []
    for offset in range(0, len(audio), frame_bytes):
        frame = audio[offset : offset + frame_bytes]
        if len(frame) == frame_bytes:
            frames.append(frame)
            continue
        if pad_last:
            frames.append(frame + b"\x00" * (frame_bytes - len(frame)))
            continue
        raise ValueError("audio length is not aligned to frame_bytes")
    return frames


def pcm_s16le_to_pcma(pcm: bytes) -> bytes:
    return bytes(_linear_to_alaw(sample) for sample in pcm_s16le_to_samples(pcm))


def pcma_to_pcm_s16le(pcma: bytes) -> bytes:
    return samples_to_pcm_s16le(_alaw_to_linear(value) for value in pcma)


def _upsample_linear(
    samples: Sequence[int],
    source_rate: int,
    target_rate: int,
    output_count: int,
) -> list[int]:
    output: list[int] = []
    for output_index in range(output_count):
        source_position = output_index * source_rate / target_rate
        left_index = int(math.floor(source_position))
        if left_index >= len(samples) - 1:
            output.append(_clamp_int16(samples[-1]))
            continue

        fraction = source_position - left_index
        left = samples[left_index]
        right = samples[left_index + 1]
        output.append(_clamp_int16(round(left + (right - left) * fraction)))
    return output


def _downsample_average(
    samples: Sequence[int],
    source_rate: int,
    target_rate: int,
    output_count: int,
) -> list[int]:
    source_per_output = source_rate / target_rate
    output: list[int] = []

    for output_index in range(output_count):
        start = output_index * source_per_output
        end = min((output_index + 1) * source_per_output, len(samples))
        first_index = int(math.floor(start))
        last_index = int(math.ceil(end))

        weighted_sum = 0.0
        total_weight = 0.0
        for source_index in range(first_index, last_index):
            segment_start = max(start, source_index)
            segment_end = min(end, source_index + 1)
            weight = max(0.0, segment_end - segment_start)
            if weight == 0:
                continue
            weighted_sum += samples[source_index] * weight
            total_weight += weight

        if total_weight == 0:
            output.append(_clamp_int16(samples[min(first_index, len(samples) - 1)]))
            continue
        output.append(_clamp_int16(round(weighted_sum / total_weight)))

    return output


def _linear_to_alaw(sample: int) -> int:
    pcm_value = _clamp_int16(sample) >> 3
    if pcm_value >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm_value = -pcm_value - 1

    segment = _search_alaw_segment(pcm_value)
    if segment >= 8:
        return 0x7F ^ mask

    encoded = segment << 4
    if segment < 2:
        encoded |= (pcm_value >> 1) & 0x0F
    else:
        encoded |= (pcm_value >> segment) & 0x0F
    return encoded ^ mask


def _alaw_to_linear(value: int) -> int:
    value ^= 0x55
    magnitude = (value & 0x0F) << 4
    segment = (value & 0x70) >> 4

    if segment == 0:
        magnitude += 8
    elif segment == 1:
        magnitude += 0x108
    else:
        magnitude += 0x108
        magnitude <<= segment - 1

    if value & 0x80:
        return magnitude
    return -magnitude


def _search_alaw_segment(value: int) -> int:
    for index, end in enumerate(_ALAW_SEG_END):
        if value <= end:
            return index
    return len(_ALAW_SEG_END)


def _validate_pcm_s16le(pcm: bytes) -> None:
    if len(pcm) % SAMPLE_WIDTH_BYTES != 0:
        raise ValueError("pcm_s16le byte length must be even")


def _validate_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _clamp_int16(value: int | float) -> int:
    return min(INT16_MAX, max(INT16_MIN, int(value)))


def _float_sample_to_int16(value: float) -> int:
    if math.isnan(value):
        return 0
    if value <= -1.0:
        return INT16_MIN
    if value >= 1.0:
        return INT16_MAX
    return round(value * INT16_MAX)
