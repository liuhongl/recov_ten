from __future__ import annotations

import pytest

from app.config import FreeSwitchConfig
from app.media_contract import PhoneMediaContract, build_realtime_phone_contract


def test_realtime_phone_contract_matches_pcma_telephony_target():
    contract = build_realtime_phone_contract(FreeSwitchConfig())

    assert contract.codec == "PCMA"
    assert contract.sample_rate == 8000
    assert contract.channels == 1
    assert contract.frame_duration_ms == 20
    assert contract.samples_per_frame == 160
    assert contract.pcm_frame_bytes == 320
    assert contract.encoded_payload_bytes == 160
    assert contract.log_fields() == {
        "phone_codec": "PCMA",
        "sample_rate": 8000,
        "channels": 1,
        "frame_duration_ms": 20,
        "pcm_frame_bytes": 320,
        "encoded_payload_bytes": 160,
    }


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            FreeSwitchConfig(phone_codec="PCMU"),
            "phone_codec=PCMA",
        ),
        (
            FreeSwitchConfig(sample_rate=16000),
            "sample_rate=8000",
        ),
        (
            FreeSwitchConfig(channels=2),
            "channels=1",
        ),
        (
            FreeSwitchConfig(frame_duration_ms=40),
            "frame_duration_ms=20",
        ),
    ],
)
def test_realtime_phone_contract_rejects_non_target_media(
    config: FreeSwitchConfig,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        build_realtime_phone_contract(config)


def test_contract_can_report_pcms_for_supported_g711_variants():
    contract = PhoneMediaContract(
        codec="PCMU",
        sample_rate=8000,
        channels=1,
        frame_duration_ms=20,
    )

    assert contract.encoded_payload_bytes == 160
