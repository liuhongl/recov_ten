import asyncio
import audioop
from array import array
import json
import sys
from pathlib import Path

import pytest


EXTENSION_ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROOT = Path(__file__).resolve().parents[4]
TENAPP_SYSTEM = (
    AGENTS_ROOT
    / "examples"
    / "voice-assistant-sip-trunk"
    / "tenapp"
    / "ten_packages"
    / "system"
)

sys.path.insert(0, str(EXTENSION_ROOT))
sys.path.insert(0, str(TENAPP_SYSTEM / "ten_ai_base" / "interface"))
sys.path.insert(0, str(TENAPP_SYSTEM / "ten_runtime_python" / "interface"))

from sip_media_bridge.extension import SipMediaBridgeExtension  # noqa: E402
from sip_media_bridge import extension as bridge_module  # noqa: E402


class FakeData:
    def __init__(self, name="tts_flush"):
        self.name = name

    def get_name(self):
        return self.name

    def get_property_to_json(self, _path):
        return json.dumps({"flush_id": "interrupt-1"}), None


class FakeTenEnv:
    def __init__(self):
        self.sent_data = []
        self.sent_audio_frames = []

    def log_info(self, *_args, **_kwargs):
        pass

    def log_warn(self, *_args, **_kwargs):
        pass

    async def send_data(self, data):
        payload, _ = data.get_property_to_json(None)
        self.sent_data.append(
            {
                "name": data.get_name(),
                "payload": json.loads(payload or "{}"),
            }
        )

    async def send_audio_frame(self, audio_frame=None, **_kwargs):
        self.sent_audio_frames.append(audio_frame)


class FakeAudioFrame:
    def __init__(self, payload, sample_rate=16000):
        self.payload = payload
        self.sample_rate = sample_rate

    def get_buf(self):
        return self.payload

    def get_sample_rate(self):
        return self.sample_rate

    def get_number_of_channels(self):
        return 1

    def get_bytes_per_sample(self):
        return 2


def _mono_pcm_frame(amplitude, samples=160):
    return array("h", [amplitude] * samples).tobytes()


def test_tts_flush_invalidates_pending_outgoing_audio():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    before = bridge._flush_generation
    bridge._downsample_2x_pending_sample = 123
    bridge._outgoing_pcm_pending = b"123"

    asyncio.run(bridge.on_data(FakeTenEnv(), FakeData()))

    assert bridge._flush_generation == before + 1
    assert bridge._should_abort_outgoing_send(before)
    assert bridge._downsample_2x_pending_sample is None
    assert bridge._outgoing_pcm_pending == b""


def test_output_level_applies_gain_and_peak_limit():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.output_gain = 1.0
    bridge.config.output_peak_limit = 12000
    payload = array("h", [20000, -20000, 1000, -1000]).tobytes()

    adjusted = bridge._apply_output_level(payload, FakeTenEnv())

    assert audioop.max(adjusted, 2) <= 12000


def test_16k_outgoing_audio_uses_low_pass_2x_downsample():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    input_pcm = array("h", [10000, -10000, 8000, -8000]).tobytes()

    normalized = bridge._normalize_outgoing_pcm(
        FakeAudioFrame(input_pcm, sample_rate=16000),
        FakeTenEnv(),
    )

    assert normalized is not None
    assert len(normalized) == len(input_pcm) // 2
    assert audioop.max(normalized, 2) == 0


def test_2x_downsample_keeps_odd_sample_for_next_chunk():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    first = array("h", [10000, -10000, 8000, -8000, 6000]).tobytes()
    second = array("h", [-6000, 4000, -4000]).tobytes()

    first_out = bridge._downsample_2x_mono_16bit(first)
    second_out = bridge._downsample_2x_mono_16bit(second)

    assert list(array("h", first_out)) == [0, 0]
    assert list(array("h", second_out)) == [0, 0]
    assert bridge._downsample_2x_pending_sample is None


def test_outgoing_dump_writes_pcm(tmp_path):
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.dump = True
    bridge.config.dump_path = str(tmp_path)
    bridge.config.channel = "test"

    bridge._init_outgoing_dump(FakeTenEnv())
    bridge._dump_outgoing_pcm(b"1234", FakeTenEnv())

    dump_files = list(tmp_path.glob("*.pcm"))
    assert len(dump_files) == 1
    assert dump_files[0].read_bytes() == b"1234"


def test_outgoing_chunks_do_not_pad_between_tts_packets():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    frame_size = 320

    first_chunks = bridge._append_outgoing_pcm(b"a" * 100, frame_size)
    second_chunks = bridge._append_outgoing_pcm(b"b" * 220, frame_size)

    assert first_chunks == []
    assert second_chunks == [b"a" * 100 + b"b" * 220]
    assert bridge._outgoing_pcm_pending == b""


def test_outgoing_pacer_compensates_send_overhead_across_batches(monkeypatch):
    clock = {"now": 0.0}
    sent_at = []

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        async def send(self, _chunk):
            sent_at.append(clock["now"])
            clock["now"] += 0.002

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    frame_size = bridge._outgoing_frame_size_bytes()

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(b"a" * frame_size * 2, sample_rate=8000),
        )
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(b"b" * frame_size * 2, sample_rate=8000),
        )

    asyncio.run(exercise())

    assert sent_at == pytest.approx([0.06, 0.08, 0.1, 0.12], abs=0.0001)


def test_outgoing_pacer_buffers_start_to_absorb_delayed_tts_batch(monkeypatch):
    clock = {"now": 0.0}
    sent_at = []

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        async def send(self, _chunk):
            sent_at.append(clock["now"])

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    frame_size = bridge._outgoing_frame_size_bytes()

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(b"a" * frame_size * 3, sample_rate=8000),
        )
        clock["now"] = 0.12
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(b"b" * frame_size * 2, sample_rate=8000),
        )

    asyncio.run(exercise())

    gaps = [sent_at[index] - sent_at[index - 1] for index in range(1, len(sent_at))]

    assert sent_at[0] == pytest.approx(0.06, abs=0.0001)
    assert max(gaps) <= 0.021


def test_outgoing_silence_trim_removes_tts_leading_and_trailing_silence(
    monkeypatch,
):
    clock = {"now": 0.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, chunk):
            self.sent.append(chunk)

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge.config.outgoing_silence_trim_enabled = True
    bridge.config.outgoing_silence_rms_threshold = 120
    bridge.config.outgoing_silence_max_hold_ms = 1000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    frame_size = bridge._outgoing_frame_size_bytes()
    silence = b"\x00" * frame_size
    voice = _mono_pcm_frame(1000)

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(silence * 3 + voice * 2 + silence * 4, sample_rate=8000),
        )
        await bridge.on_data(FakeTenEnv(), FakeData("tts_audio_end"))

    asyncio.run(exercise())

    assert bridge._ws.sent == [voice, voice]


def test_outgoing_silence_trim_preserves_internal_short_pause(monkeypatch):
    clock = {"now": 0.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, chunk):
            self.sent.append(chunk)

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge.config.outgoing_silence_trim_enabled = True
    bridge.config.outgoing_silence_rms_threshold = 120
    bridge.config.outgoing_silence_max_hold_ms = 1000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    frame_size = bridge._outgoing_frame_size_bytes()
    silence = b"\x00" * frame_size
    voice = _mono_pcm_frame(1000)

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(voice + silence * 3 + voice, sample_rate=8000),
        )
        await bridge.on_data(FakeTenEnv(), FakeData("tts_audio_end"))

    asyncio.run(exercise())

    assert bridge._ws.sent == [voice, silence, silence, silence, voice]


def test_outgoing_silence_trim_flushes_low_energy_tail_on_final(monkeypatch):
    clock = {"now": 0.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, chunk):
            self.sent.append(chunk)

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge.config.outgoing_silence_trim_enabled = True
    bridge.config.outgoing_silence_rms_threshold = 120
    bridge.config.outgoing_silence_max_hold_ms = 1000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    low_energy_tail = _mono_pcm_frame(60)
    voice = _mono_pcm_frame(1000)

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(voice + low_energy_tail * 3, sample_rate=8000),
        )
        await bridge.on_data(FakeTenEnv(), FakeData("tts_audio_end"))

    asyncio.run(exercise())

    assert bridge._ws.sent == [
        voice,
        low_energy_tail,
        low_energy_tail,
        low_energy_tail,
    ]


def test_default_outgoing_path_preserves_synthesized_silence_timeline(monkeypatch):
    clock = {"now": 0.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, chunk):
            self.sent.append(chunk)

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    frame_size = bridge._outgoing_frame_size_bytes()
    silence = b"\x00" * frame_size
    voice = _mono_pcm_frame(1000)

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(
                silence + voice + silence + voice + silence,
                sample_rate=8000,
            ),
        )
        await bridge.on_data(FakeTenEnv(), FakeData("tts_audio_end"))

    asyncio.run(exercise())

    assert bridge._ws.sent == [silence, voice, silence, voice, silence]


def test_default_outgoing_silence_trim_keeps_low_energy_tts_audio(monkeypatch):
    clock = {"now": 0.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, chunk):
            self.sent.append(chunk)

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = True
    low_energy_speech = _mono_pcm_frame(60)

    async def exercise():
        await bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(low_energy_speech * 3, sample_rate=8000),
        )

    asyncio.run(exercise())

    assert bridge._ws.sent == [
        low_energy_speech,
        low_energy_speech,
        low_energy_speech,
    ]


def test_peer_disconnected_control_notifies_graph_and_flushes_outgoing_state():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge._outgoing_pcm_pending = b"pending"
    bridge._downsample_2x_pending_sample = 123
    before = bridge._flush_generation
    ten_env = FakeTenEnv()

    asyncio.run(
        bridge._handle_media_hub_control_message(
            {"type": "peer_disconnected", "peer_role": "fs", "reason": "hangup"},
            ten_env,
        )
    )

    assert bridge._flush_generation == before + 1
    assert bridge._outgoing_pcm_pending == b""
    assert bridge._downsample_2x_pending_sample is None
    assert ten_env.sent_data == [
        {
            "name": "sip_peer_disconnected",
            "payload": {
                "channel": "default",
                "peer_role": "fs",
                "reason": "hangup",
            },
        }
    ]


def test_outgoing_audio_is_dropped_when_fs_peer_is_disconnected():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, chunk):
            self.sent.append(chunk)

    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge._ws = FakeWebSocket()
    bridge._media_peer_connected = False
    frame_size = bridge._outgoing_frame_size_bytes()

    asyncio.run(
        bridge.on_audio_frame(
            FakeTenEnv(),
            FakeAudioFrame(b"a" * frame_size, sample_rate=8000),
        )
    )

    assert bridge._ws.sent == []


def test_inbound_speech_start_event_after_sustained_voice():
    bridge = SipMediaBridgeExtension("sip_media_bridge")
    bridge.config.sample_rate = 8000
    bridge.config.speech_start_min_rms = 80
    bridge.config.speech_start_min_peak = 500
    bridge.config.speech_start_min_frames = 3
    bridge.config.speech_start_rearm_silence_frames = 5
    bridge.config.speech_start_cooldown_ms = 0
    bridge._outgoing_pcm_pending = b"pending"
    bridge._downsample_2x_pending_sample = 123
    before_generation = bridge._flush_generation
    ten_env = FakeTenEnv()

    async def exercise():
        await bridge._send_pcm_to_graph(_mono_pcm_frame(1000), ten_env)
        await bridge._send_pcm_to_graph(_mono_pcm_frame(1000), ten_env)
        assert ten_env.sent_data == []

        await bridge._send_pcm_to_graph(_mono_pcm_frame(1000), ten_env)

    asyncio.run(exercise())

    assert [item["name"] for item in ten_env.sent_data] == [
        "sip_user_speech_start"
    ]
    assert ten_env.sent_data[0]["payload"]["channel"] == "default"
    assert ten_env.sent_data[0]["payload"]["rms"] >= 80
    assert len(ten_env.sent_audio_frames) == 3
    assert bridge._flush_generation == before_generation
    assert bridge._outgoing_pcm_pending == b"pending"
    assert bridge._downsample_2x_pending_sample == 123
