from cosy_tts_python.config import CosyTTSConfig


def test_speech_rate_can_be_loaded_from_params():
    config = CosyTTSConfig(
        api_key="key",
        model="cosyvoice-v3-flash",
        voice="longanyang",
        params={"speech_rate": 1.15},
    )

    config.update_params()

    assert config.speech_rate == 1.15
