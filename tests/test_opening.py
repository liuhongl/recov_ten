from __future__ import annotations

import pytest

from app.audio_codec import samples_to_pcm_s16le
from app.config import FreeSwitchConfig, GatewayConfig, PlaybackConfig
from app.opening import (
    OpeningAudio,
    OpeningAudioStore,
    OpeningGenerationFailed,
    build_prepared_opening_audio,
    parse_opening_request,
)


def test_parse_opening_request_renders_fixed_template_and_hashes_text():
    opening = parse_opening_request(
        {
            "voice": "female",
            "business": {
                "owner_name": "测试业主",
                "arrears_amount": "12.34",
            },
        }
    )

    assert opening is not None
    assert opening.voice == "female"
    assert opening.speaker == "zh_female_vv_jupiter_bigtts"
    assert (
        opening.opening_text
        == "您好，请问是测试业主吗？系统显示您当前有12.34元待缴费用，想和您确认一下。"
    )
    assert len(opening.opening_text_hash) == 64


def test_parse_opening_request_rejects_missing_business_field():
    with pytest.raises(OpeningGenerationFailed, match="owner_name is required"):
        parse_opening_request(
            {
                "voice": "female",
                "business": {
                    "arrears_amount": "12.34",
                },
            }
        )


def test_build_prepared_opening_audio_resamples_to_phone_frames_and_adds_tail():
    opening = parse_opening_request(
        {
            "voice": "male",
            "business": {
                "owner_name": "测试业主",
                "arrears_amount": "12.34",
            },
        }
    )
    assert opening is not None
    source_audio = samples_to_pcm_s16le([1000] * 480)

    prepared = build_prepared_opening_audio(
        call_id="call-1",
        opening=opening,
        audio=OpeningAudio(
            pcm16=source_audio,
            sample_rate=24000,
            generation_ms=1234,
        ),
        config=GatewayConfig(
            freeswitch=FreeSwitchConfig(sample_rate=8000, frame_duration_ms=20),
            playback=PlaybackConfig(tail_silence_ms=40),
        ),
    )

    assert prepared.call_id == "call-1"
    assert prepared.voice == "male"
    assert prepared.opening_text == opening.opening_text
    assert prepared.opening_text_hash == opening.opening_text_hash
    assert prepared.source_sample_rate == 24000
    assert prepared.source_audio_bytes == len(source_audio)
    assert prepared.generation_ms == 1234
    assert len(prepared.phone_frames) == 3
    assert all(len(frame) == 320 for frame in prepared.phone_frames)
    assert prepared.phone_frames[-2:] == [b"\x00" * 320, b"\x00" * 320]


def test_opening_audio_store_pops_by_call_id():
    store = OpeningAudioStore()
    opening = parse_opening_request(
        {
            "business": {
                "owner_name": "测试业主",
                "arrears_amount": "12.34",
            },
        }
    )
    assert opening is not None
    prepared = build_prepared_opening_audio(
        call_id="call-1",
        opening=opening,
        audio=OpeningAudio(
            pcm16=samples_to_pcm_s16le([1000] * 160),
            sample_rate=8000,
            generation_ms=1,
        ),
        config=GatewayConfig(playback=PlaybackConfig(tail_silence_ms=0)),
    )

    store.put(prepared)

    assert store.pop("call-1") is prepared
    assert store.pop("call-1") is None
