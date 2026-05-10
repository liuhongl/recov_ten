import asyncio
import json
import sys
from pathlib import Path


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

from aliyun_asr_bigmodel_python.config import (  # noqa: E402
    AliyunASRBigmodelConfig,
)
from aliyun_asr_bigmodel_python.extension import (  # noqa: E402
    AliyunASRBigmodelExtension,
)


class FakeData:
    def __init__(self, payload, name):
        self.payload = payload
        self.name = name

    def get_name(self):
        return self.name

    def get_property_to_json(self, _path=None):
        return json.dumps(self.payload), None


class FakeTenEnv:
    def __init__(self):
        self.sent_data = []
        self.logs = []

    def log_info(self, message, *_args, **_kwargs):
        self.logs.append(("info", message))

    def log_warn(self, message, *_args, **_kwargs):
        self.logs.append(("warn", message))

    def log_error(self, message, *_args, **_kwargs):
        self.logs.append(("error", message))

    def log_debug(self, message, *_args, **_kwargs):
        self.logs.append(("debug", message))

    async def send_data(self, data):
        payload, _ = data.get_property_to_json(None)
        self.sent_data.append(
            {
                "name": data.get_name(),
                "payload": json.loads(payload or "{}"),
            }
        )


class FakeRecognition:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class FakeReconnectManager:
    def __init__(self):
        self.attempts = 0

    def can_retry(self):
        return True

    async def handle_reconnect(self, **_kwargs):
        self.attempts += 1
        return True


class FakeAudioFrame:
    def __init__(self, payload):
        self.payload = payload

    def get_buf(self):
        return self.payload


def run(coro):
    return asyncio.run(coro)


def make_config():
    return AliyunASRBigmodelConfig.model_validate_json("{}")


def test_peer_disconnected_stops_asr_and_disables_reconnect():
    extension = AliyunASRBigmodelExtension("stt")
    ten_env = FakeTenEnv()
    recognition = FakeRecognition()
    reconnect_manager = FakeReconnectManager()

    extension.ten_env = ten_env
    extension.config = make_config()
    extension.connected = True
    extension.recognition = recognition
    extension.reconnect_manager = reconnect_manager

    extension.buffered_frames.put_nowait(FakeAudioFrame(b"old-buffer"))
    extension.buffered_frames_size = len(b"old-buffer")
    extension.audio_frames_queue.put_nowait(FakeAudioFrame(b"old-queue"))

    run(
        extension.on_data(
            ten_env,
            FakeData(
                {"peer_role": "fs", "reason": "connection_closed"},
                "sip_peer_disconnected",
            ),
        )
    )

    assert recognition.stop_calls == 1
    assert extension.is_connected() is False
    assert extension.peer_connected is False
    assert extension.reconnect_enabled is False
    assert extension.buffered_frames.qsize() == 0
    assert extension.audio_frames_queue.qsize() == 0
    assert extension.buffered_frames_size == 0

    extension.connected = True
    run(extension.on_asr_reconnection())

    assert reconnect_manager.attempts == 0


def test_peer_connected_restarts_asr_for_next_call():
    extension = AliyunASRBigmodelExtension("stt")
    ten_env = FakeTenEnv()
    starts = []

    async def fake_start_connection():
        starts.append(True)
        extension.connected = True

    extension.ten_env = ten_env
    extension.config = make_config()
    extension.peer_connected = False
    extension.reconnect_enabled = False
    extension.start_connection = fake_start_connection

    run(
        extension.on_data(
            ten_env,
            FakeData({"peer_role": "fs"}, "sip_peer_connected"),
        )
    )

    assert starts == [True]
    assert extension.peer_connected is True
    assert extension.reconnect_enabled is True
    assert extension.is_connected() is False


def test_late_asr_result_after_peer_disconnect_is_ignored():
    extension = AliyunASRBigmodelExtension("stt")
    ten_env = FakeTenEnv()
    extension.ten_env = ten_env
    extension.config = make_config()
    extension.peer_connected = False

    run(
        extension._handle_asr_result(
            text="迟到的识别结果。",
            final=True,
            start_ms=1000,
            duration_ms=800,
            language="zh-CN",
        )
    )

    assert ten_env.sent_data == []
