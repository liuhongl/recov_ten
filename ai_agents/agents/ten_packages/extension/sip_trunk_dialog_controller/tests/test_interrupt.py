import asyncio
import json
import sys
import time
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

from ten_ai_base.struct import LLMMessageContent  # noqa: E402
from sip_trunk_dialog_controller.extension import (  # noqa: E402
    SipTrunkDialogControllerExtension,
    _parse_sentences,
)


class FakeData:
    def __init__(self, payload, name="asr_result"):
        self.payload = payload
        self.name = name

    def get_name(self):
        return self.name

    def get_property_to_json(self, _path):
        return json.dumps(self.payload), None


class FakeTenEnv:
    def __init__(self):
        self.sent_data = []

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


def run(coro):
    return asyncio.run(coro)


def test_partial_asr_does_not_interrupt_when_ai_is_idle():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    calls = []

    async def fake_send_interrupt_flush(text):
        calls.append(text)

    extension._send_interrupt_flush = fake_send_interrupt_flush
    extension.config.interrupt_on_partial = True
    extension.config.interrupt_min_chars = 2
    extension.config.interrupt_min_duration_ms = 600

    partial = FakeData(
        {
            "text": "你好",
            "final": False,
            "duration_ms": 1200,
            "metadata": {"session_id": "0"},
        }
    )

    run(extension.on_data(FakeTenEnv(), partial))

    assert calls == []


def test_stop_command_partial_interrupts_immediately_during_ai_output():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()

    async def scenario():
        extension.ten_env = ten_env
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "停",
                    "final": False,
                    "duration_ms": 120,
                    "metadata": {"session_id": "0"},
                }
            ),
        )

    run(scenario())

    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert ten_env.sent_data[0]["payload"]["metadata"]["reason"] == (
        "urgent_partial_asr"
    )
    assert ten_env.sent_data[0]["payload"]["metadata"]["text"] == "停"


def test_user_speech_start_marks_candidate_without_cutting_audio():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()

    async def scenario():
        extension.ten_env = ten_env
        extension.contexts.append(LLMMessageContent(role="user", content="你好。"))
        extension.contexts.append(
            LLMMessageContent(role="assistant", content="您好，有什么可以帮您？")
        )
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 2200, "peak": 6500},
                "sip_user_speech_start",
            ),
        )

    run(scenario())

    assert [message.role for message in extension.contexts] == ["user", "assistant"]
    assert ten_env.sent_data == []


def test_final_asr_interrupt_removes_interrupted_assistant_context():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.contexts.append(LLMMessageContent(role="user", content="你是谁？"))
        extension.contexts.append(
            LLMMessageContent(role="assistant", content="我是您的语音助手。")
        )
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "你卡断了。",
                    "final": True,
                    "duration_ms": 1300,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert [message.role for message in extension.contexts] == ["user"]
    assert calls == ["你卡断了。"]
    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert ten_env.sent_data[0]["payload"]["metadata"]["reason"] == "final_asr"


def test_stop_command_final_after_partial_interrupt_does_not_start_llm_turn():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "停",
                    "final": False,
                    "duration_ms": 120,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "停。",
                    "final": True,
                    "duration_ms": 300,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert extension.turn_id == 0


def test_user_speech_start_does_not_interrupt_ai_output_before_asr_text():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()

    async def scenario():
        extension.ten_env = ten_env
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 2200, "peak": 6500},
                "sip_user_speech_start",
            ),
        )

    run(scenario())

    assert ten_env.sent_data == []
    assert extension.interrupt_flush_sent is False


def test_low_energy_user_speech_start_during_ai_output_is_ignored_as_echo():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()

    async def scenario():
        extension.ten_env = ten_env
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 606, "peak": 1756},
                "sip_user_speech_start",
            ),
        )

    run(scenario())

    assert ten_env.sent_data == []
    assert extension.interrupt_flush_sent is False


def test_user_speech_start_during_llm_before_audio_does_not_cancel_turn():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()

    async def scenario():
        extension.ten_env = ten_env
        task = asyncio.create_task(asyncio.sleep(60))
        extension.current_llm_task = task

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 528, "peak": 988},
                "sip_user_speech_start",
            ),
        )
        await asyncio.sleep(0)
        sent_data = list(ten_env.sent_data)
        task_cancelled = task.cancelled()

        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return sent_data, task_cancelled

    sent_data, task_cancelled = run(scenario())

    assert sent_data == []
    assert task_cancelled is False
    assert extension.interrupt_flush_sent is False


def test_final_asr_after_speech_start_barge_in_starts_turn_without_second_flush():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 2200, "peak": 6500},
                "sip_user_speech_start",
            ),
        )
        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "讲一下天外村。",
                    "final": True,
                    "duration_ms": 1300,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == ["讲一下天外村。"]
    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert extension.turn_id == 1


def test_short_final_asr_after_speech_start_barge_in_is_ignored_as_echo_tail():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 2200, "peak": 6500},
                "sip_user_speech_start",
            ),
        )
        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "请。",
                    "final": True,
                    "duration_ms": 520,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert ten_env.sent_data == []
    assert extension.turn_id == 0


def test_stop_final_after_speech_start_barge_in_does_not_start_llm_turn():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 3

        await extension.on_data(
            ten_env,
            FakeData(
                {"channel": "default", "rms": 2200, "peak": 6500},
                "sip_user_speech_start",
            ),
        )
        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "停。",
                    "final": True,
                    "duration_ms": 360,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert ten_env.sent_data[0]["payload"]["metadata"]["reason"] == (
        "urgent_final_asr"
    )
    assert extension.turn_id == 0


def test_partial_asr_without_speech_start_candidate_does_not_interrupt():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    calls = []

    async def fake_send_interrupt_flush(text, reason="partial_asr"):
        calls.append((text, reason))

    extension._send_interrupt_flush = fake_send_interrupt_flush
    extension.config.interrupt_on_partial = True
    extension.config.interrupt_min_chars = 2
    extension.config.interrupt_min_duration_ms = 600
    extension.tts_playing = True

    stable_partial = FakeData(
        {
            "text": "你好",
            "final": False,
            "duration_ms": 700,
            "metadata": {"session_id": "0"},
        }
    )

    run(extension.on_data(FakeTenEnv(), stable_partial))

    assert calls == []


def test_partial_asr_waits_for_stable_user_takeover_before_interrupt():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    calls = []

    async def fake_send_interrupt_flush(text, reason="partial_asr"):
        calls.append((text, reason))

    extension._send_interrupt_flush = fake_send_interrupt_flush
    extension.config.interrupt_on_partial = True
    extension.config.interrupt_min_chars = 2
    extension.config.interrupt_min_duration_ms = 600
    extension.tts_playing = True
    extension._mark_barge_in_candidate()

    short_partial = FakeData(
        {
            "text": "你",
            "final": False,
            "duration_ms": 500,
            "metadata": {"session_id": "0"},
        }
    )
    stable_partial = FakeData(
        {
            "text": "你好",
            "final": False,
            "duration_ms": 700,
            "metadata": {"session_id": "0"},
        }
    )

    run(extension.on_data(FakeTenEnv(), short_partial))
    run(extension.on_data(FakeTenEnv(), stable_partial))
    run(extension.on_data(FakeTenEnv(), stable_partial))

    assert calls == [("你好", "partial_asr")]


def test_sentence_parser_waits_for_sentence_final_punctuation():
    sentences, fragment = _parse_sentences("", "您好，")
    assert sentences == []
    assert fragment == "您好，"

    sentences, fragment = _parse_sentences(fragment, "有什么可以帮您？")
    assert sentences == ["您好，有什么可以帮您？"]
    assert fragment == ""


def test_tts_events_track_whether_ai_is_playing():
    extension = SipTrunkDialogControllerExtension("dialog_controller")

    run(
        extension.on_data(
            FakeTenEnv(),
            FakeData({"request_id": "sip-trunk-tts-1"}, "tts_audio_start"),
        )
    )

    assert extension.tts_playing is True

    run(
        extension.on_data(
            FakeTenEnv(),
            FakeData({"request_id": "sip-trunk-tts-1"}, "tts_audio_end"),
        )
    )

    assert extension.tts_playing is False


def test_final_asr_flushes_recent_tts_playback_before_new_turn():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    calls = []

    async def fake_send_interrupt_flush(text, reason="partial_asr"):
        calls.append((text, reason))

    async def fake_run_llm_and_tts(_text):
        return None

    async def scenario():
        extension._send_interrupt_flush = fake_send_interrupt_flush
        extension._run_llm_and_tts = fake_run_llm_and_tts

        await extension.on_data(
            FakeTenEnv(),
            FakeData({"request_id": "sip-trunk-tts-1"}, "tts_audio_start"),
        )
        await extension.on_data(
            FakeTenEnv(),
            FakeData(
                {
                    "request_id": "sip-trunk-tts-1",
                    "request_total_audio_duration_ms": 2000,
                },
                "tts_audio_end",
            ),
        )
        await extension.on_data(
            FakeTenEnv(),
            FakeData(
                {
                    "text": "我想问个事。",
                    "final": True,
                    "duration_ms": 900,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == [("我想问个事。", "final_asr")]


def test_final_asr_near_tts_tail_does_not_flush_to_avoid_clipping():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    calls = []

    async def fake_send_interrupt_flush(text, reason="partial_asr"):
        calls.append((text, reason))

    async def fake_run_llm_and_tts(_text):
        return None

    async def scenario():
        extension._send_interrupt_flush = fake_send_interrupt_flush
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.config.final_interrupt_min_remaining_ms = 700
        extension.tts_output_active_until = time.monotonic() + 0.4

        await extension.on_data(
            FakeTenEnv(),
            FakeData(
                {
                    "text": "行不？",
                    "final": True,
                    "duration_ms": 760,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []


def test_short_final_asr_during_ai_tail_does_not_start_new_turn():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 0.4

        await extension.on_data(
            FakeTenEnv(),
            FakeData(
                {
                    "text": "你。",
                    "final": True,
                    "duration_ms": 760,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert extension.turn_id == 0


def test_short_final_asr_during_ai_output_does_not_cut_playback():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 1.5

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "你好。",
                    "final": True,
                    "duration_ms": 660,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert ten_env.sent_data == []
    assert extension.turn_id == 0


def test_two_char_final_asr_at_900ms_during_ai_output_does_not_cut_playback():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 1.5

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "请问。",
                    "final": True,
                    "duration_ms": 900,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert ten_env.sent_data == []
    assert extension.turn_id == 0


def test_echo_like_final_asr_during_ai_output_is_ignored():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 1.5

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "可可以。",
                    "final": True,
                    "duration_ms": 780,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert ten_env.sent_data == []
    assert extension.turn_id == 0


def test_substantial_final_asr_during_ai_output_can_interrupt():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.tts_output_active_until = time.monotonic() + 1.5

        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "讲一下天外村。",
                    "final": True,
                    "duration_ms": 1300,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == ["讲一下天外村。"]
    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert ten_env.sent_data[0]["payload"]["metadata"]["reason"] == "final_asr"


def test_tts_output_budget_limits_spoken_text_in_code():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    extension.ten_env = ten_env
    extension.turn_id = 1
    extension.config.max_spoken_chars_per_turn = 6

    run(extension._send_to_tts("这是第一句，后面很多。", False))
    run(extension._send_to_tts("不应该继续播。", False))
    run(extension._send_to_tts("", True))

    assert [item["payload"]["text"] for item in ten_env.sent_data] == [
        "这是第一句。",
        "",
    ]
    assert ten_env.sent_data[0]["payload"]["text_input_end"] is False
    assert ten_env.sent_data[1]["payload"]["text_input_end"] is True


def test_peer_disconnected_flushes_ai_and_ignores_late_final_asr():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    calls = []

    async def fake_run_llm_and_tts(text):
        calls.append(text)

    async def scenario():
        extension.ten_env = ten_env
        extension._run_llm_and_tts = fake_run_llm_and_tts
        extension.pending_tts_output = True

        await extension.on_data(
            ten_env,
            FakeData(
                {"peer_role": "fs", "reason": "connection_closed"},
                "sip_peer_disconnected",
            ),
        )
        await extension.on_data(
            ten_env,
            FakeData(
                {
                    "text": "行，好的，先这样，挂了。",
                    "final": True,
                    "duration_ms": 2540,
                    "metadata": {"session_id": "0"},
                }
            ),
        )
        await asyncio.sleep(0)

    run(scenario())

    assert calls == []
    assert extension.call_active is False
    assert [item["name"] for item in ten_env.sent_data] == ["tts_flush"]
    assert ten_env.sent_data[0]["payload"]["metadata"]["reason"] == "peer_disconnected"


def test_peer_connected_resets_call_state_for_next_call():
    extension = SipTrunkDialogControllerExtension("dialog_controller")
    ten_env = FakeTenEnv()
    extension.call_active = False
    extension.contexts.append({"role": "user", "content": "old"})
    extension.turn_id = 5
    extension.pending_tts_output = True

    run(
        extension.on_data(
            ten_env,
            FakeData({"peer_role": "fs"}, "sip_peer_connected"),
        )
    )

    assert extension.call_active is True
    assert extension.contexts == []
    assert extension.turn_id == 0
    assert extension.pending_tts_output is False
