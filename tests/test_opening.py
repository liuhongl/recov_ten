from __future__ import annotations

import pytest

from app.audio_codec import samples_to_pcm_s16le
from app.config import FreeSwitchConfig, GatewayConfig, PlaybackConfig
from app.doubao_s2s_client import DoubaoS2SCredentials
from app.opening import (
    OpeningAudio,
    OpeningAudioStore,
    DoubaoOpeningAudioGenerator,
    OpeningGenerationFailed,
    OpeningRequest,
    build_business_opening_request,
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
        == "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    )
    assert "12.34" not in opening.opening_text
    assert "元" not in opening.opening_text
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


def test_build_business_opening_request_renders_privacy_safe_default():
    opening = build_business_opening_request(
        employee_name="李经理",
        debtor_name="金阳",
        debtor_gender="男",
        debt_amount="12.34",
        address="测试小区一号楼",
    )

    assert opening.voice == "female"
    assert opening.speaker == "zh_female_vv_jupiter_bigtts"
    assert opening.business == {
        "employee_name": "李经理",
        "debtor_name": "金阳",
        "debtor_gender": "男",
        "debt_amount": "12.34",
        "address": "测试小区一号楼",
        "title": "先生",
        "salutation": "金先生",
    }
    assert opening.opening_text == (
        "您好，请问是金先生吗？我是李经理。"
        "这边有一项物业费事项需要和您本人核实一下，请问现在方便确认吗？"
    )
    assert len(opening.opening_text_hash) == 64


def test_build_business_opening_request_uses_empty_title_for_unknown_gender():
    opening = build_business_opening_request(
        employee_name="李经理",
        debtor_name="金阳",
        debtor_gender="",
        debt_amount="12.34",
        address="测试小区一号楼",
    )

    assert "金业主吗？" in opening.opening_text
    assert opening.business["title"] == ""
    assert opening.business["salutation"] == "金业主"


def test_build_business_opening_request_uses_strategy_opening_template():
    opening = build_business_opening_request(
        employee_name="物业中心小明",
        debtor_name="金阳",
        debtor_gender="男",
        debt_amount="1250.50",
        address="阳光花园一期1栋101室",
        speaking_style="协调型、耐心沟通的物业工作人员口吻。",
        opening_template=(
            "您好，请问是{salutation}吗？我是{employee_name}。"
            "这边有一项物业费事项需要和您本人核实一下。"
        ),
    )

    assert opening.opening_text == (
        "您好，请问是金先生吗？我是物业中心小明。"
        "这边有一项物业费事项需要和您本人核实一下。"
    )
    assert opening.speaking_style == "协调型、耐心沟通的物业工作人员口吻。"


def test_build_business_opening_request_supports_legacy_double_brace_aliases():
    opening = build_business_opening_request(
        employee_name="物业中心小明",
        debtor_name="金阳",
        debtor_gender="男",
        debt_amount="1250.50",
        address="阳光花园一期1栋101室",
        opening_template=(
            "您好，请问是{{name}}本人吗？我是{{identity_name}}。"
            "这边有一项物业费相关事项需要跟您核实。"
        ),
    )

    assert opening.opening_text == (
        "您好，请问是金先生本人吗？我是物业中心小明。"
        "这边有一项物业费相关事项需要跟您核实。"
    )


def test_build_business_opening_request_supports_camel_case_identity_alias():
    opening = build_business_opening_request(
        employee_name="物业中心小明",
        debtor_name="金阳",
        debtor_gender="男",
        debt_amount="1250.50",
        address="阳光花园一期1栋101室",
        opening_template=(
            "您好，请问是{{name}} 本人吗？我是{{identityName}} 。"
            "这边有一项物业费相关事项需要跟您核实，方便先确认一下身份吗？"
        ),
    )

    assert opening.opening_text == (
        "您好，请问是金先生 本人吗？我是物业中心小明 。"
        "这边有一项物业费相关事项需要跟您核实，方便先确认一下身份吗？"
    )


def test_build_business_opening_request_ignores_sensitive_strategy_template():
    opening = build_business_opening_request(
        employee_name="物业中心小明",
        debtor_name="金阳",
        debtor_gender="",
        debt_amount="1250.50",
        address="阳光花园一期1栋101室",
        opening_template=(
            "您好，请问是{salutation}吗？我是{employee_name}。"
            "这边想和您确认一下{address}的物业费事项，"
            "系统显示目前还有{debt_amount}元待处理。"
        ),
    )

    assert opening.opening_text == (
        "您好，请问是金业主吗？我是物业中心小明。"
        "这边有一项物业费事项需要和您本人核实一下，请问现在方便确认吗？"
    )
    assert "阳光花园" not in opening.opening_text
    assert "1250.50" not in opening.opening_text


def test_opening_audio_generator_passes_speaking_style_to_text_probe(monkeypatch):
    captured = {}

    async def fake_text_probe(credentials, session_config, **kwargs):
        captured["speaking_style"] = session_config.dialog.speaking_style
        return (
            type("Result", (), {"response_done_ms": 12, "output_sample_rate": 24000})(),
            samples_to_pcm_s16le([1000] * 240),
        )

    monkeypatch.setattr("app.opening.run_doubao_s2s_text_probe", fake_text_probe)
    generator = DoubaoOpeningAudioGenerator(
        DoubaoS2SCredentials(app_id="app-a", access_token="token-a"),
        GatewayConfig().doubao_s2s,
    )

    generator.generate(
        OpeningRequest(
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            business={},
            opening_text="您好，请问是金女士吗？",
            opening_text_hash="hash-opening",
            speaking_style="协调型、耐心沟通的物业工作人员口吻。",
        )
    )

    assert captured["speaking_style"] == "协调型、耐心沟通的物业工作人员口吻。"


def test_opening_audio_generator_uses_natural_phone_tts_prompt(monkeypatch):
    captured = {}

    async def fake_text_probe(credentials, session_config, **kwargs):
        captured["input_text"] = kwargs["input_text"]
        return (
            type("Result", (), {"response_done_ms": 12, "output_sample_rate": 24000})(),
            samples_to_pcm_s16le([1000] * 240),
        )

    monkeypatch.setattr("app.opening.run_doubao_s2s_text_probe", fake_text_probe)
    generator = DoubaoOpeningAudioGenerator(
        DoubaoS2SCredentials(app_id="app-a", access_token="token-a"),
        GatewayConfig().doubao_s2s,
    )

    generator.generate(
        OpeningRequest(
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            business={},
            opening_text="您好，请问是金女士吗？",
            opening_text_hash="hash-opening",
        )
    )

    assert "电话外呼" in captured["input_text"]
    assert "自然" in captured["input_text"]
    assert "后续实时对话" in captured["input_text"]
    assert "不要像朗读通知" in captured["input_text"]
    assert "严格朗读" not in captured["input_text"]
    assert captured["input_text"].endswith("您好，请问是金女士吗？")


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


def test_build_prepared_opening_audio_trims_leading_silence():
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
    frame_samples = 160
    source_audio = samples_to_pcm_s16le(
        [0] * frame_samples * 4 + [200] * frame_samples * 3
    )

    prepared = build_prepared_opening_audio(
        call_id="call-1",
        opening=opening,
        audio=OpeningAudio(
            pcm16=source_audio,
            sample_rate=8000,
            generation_ms=1234,
        ),
        config=GatewayConfig(
            freeswitch=FreeSwitchConfig(sample_rate=8000, frame_duration_ms=20),
            playback=PlaybackConfig(tail_silence_ms=0),
        ),
    )

    assert len(prepared.phone_frames) == 3
    assert prepared.phone_frames == [samples_to_pcm_s16le([200] * frame_samples)] * 3


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
