from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import Awaitable, Callable

import pytest
from websockets.legacy.client import connect

from app import realtime_phone_gateway as realtime_gateway
from app.audio_codec import samples_to_pcm_s16le
from app.config import (
    CallRecordingConfig,
    FeatureConfig,
    FreeSwitchConfig,
    GatewayConfig,
    HandoffConfig,
    PlaybackConfig,
    VadConfig,
)
from app.freeswitch_event_socket import PlaybackProgressEvent
from app.opening import OpeningAudioStore, PreparedOpeningAudio
from app.postgres import PromptSnapshot
from app.realtime_phone_gateway import (
    ConversationExchange,
    DIALOG_PROMPT_SOFT_LIMIT_CHARS,
    FreeSwitchRealtimeGatewayServer,
    HANDOFF_CONNECTING_PROMPT_TEXT,
    OPENING_TURN_ID,
    PlaybackFrame,
    RealtimePhoneSessionStats,
    _detect_handoff_request,
    _inbound_rms_avg,
    _record_inbound_audio_rms,
)
from app.realtime_types import RealtimeTurnResult
from app.realtime_types import RealtimeDialogConfig


def test_realtime_phone_gateway_plays_model_audio_back_to_client():
    asyncio.run(_assert_realtime_phone_gateway_roundtrip())


def test_realtime_phone_gateway_clears_playback_on_user_interrupt():
    asyncio.run(_assert_realtime_phone_gateway_interrupts_playback())


def test_realtime_phone_gateway_interrupts_pending_provider_turn_on_new_speech():
    asyncio.run(_assert_realtime_phone_gateway_interrupts_pending_provider_turn())


def test_realtime_phone_gateway_restart_clears_interrupted_capture_before_replay():
    asyncio.run(_assert_realtime_phone_gateway_restart_clears_interrupted_capture())


def test_realtime_phone_gateway_replays_interrupt_audio_after_fallback_hot_restart():
    asyncio.run(_assert_realtime_phone_gateway_replays_after_fallback_hot_restart())


def test_realtime_phone_gateway_does_not_replay_interrupt_audio_when_context_repair_fails():
    asyncio.run(_assert_realtime_phone_gateway_does_not_replay_when_context_repair_fails())


def test_realtime_phone_gateway_waits_for_slow_context_repair_without_replay():
    asyncio.run(_assert_realtime_phone_gateway_waits_for_slow_context_repair())


def test_realtime_phone_gateway_appends_tail_silence_after_turn_done():
    asyncio.run(_assert_realtime_phone_gateway_appends_tail_silence())


def test_realtime_phone_gateway_commits_after_freeswitch_playback_done():
    asyncio.run(_assert_realtime_phone_gateway_waits_for_freeswitch_completion())


def test_realtime_phone_gateway_rejects_slow_playback_send_interval():
    with pytest.raises(ValueError, match="playback.send_interval_ms"):
        FreeSwitchRealtimeGatewayServer(
            _test_config(tail_silence_ms=0, send_interval_ms=40),
            api_key="test-key",
        )


def test_realtime_phone_gateway_does_not_emit_silence_when_model_audio_lags():
    asyncio.run(_assert_realtime_phone_gateway_does_not_emit_silence_on_lag())


def test_realtime_phone_gateway_prefills_completed_opening_from_queued_frames():
    asyncio.run(_assert_realtime_phone_gateway_prefills_completed_opening())


def test_realtime_phone_gateway_plays_opening_audio_before_live_turn():
    asyncio.run(_assert_realtime_phone_gateway_plays_opening_audio())


def test_realtime_phone_gateway_marks_result_failed_when_realtime_connect_fails():
    asyncio.run(_assert_realtime_phone_gateway_marks_failed_on_connect_error())


def test_realtime_phone_gateway_writes_recorded_call_opening_source_wav(tmp_path):
    asyncio.run(_assert_realtime_phone_gateway_writes_opening_source_wav(tmp_path))


def test_realtime_phone_gateway_adds_recording_opening_warmup():
    asyncio.run(_assert_realtime_phone_gateway_adds_recording_opening_warmup())


def test_realtime_phone_gateway_waits_for_answer_before_opening_audio():
    asyncio.run(_assert_realtime_phone_gateway_waits_for_answer_before_opening_audio())


def test_realtime_phone_gateway_allows_opening_audio_interruption():
    asyncio.run(_assert_realtime_phone_gateway_interrupts_opening_audio())


def test_realtime_phone_gateway_locally_interrupts_opening_before_provider_vad():
    asyncio.run(_assert_realtime_phone_gateway_locally_interrupts_opening())


def test_realtime_phone_gateway_ignores_opening_playback_echo():
    asyncio.run(_assert_realtime_phone_gateway_ignores_opening_playback_echo())


def test_realtime_phone_gateway_ignores_opening_barge_in_before_playback_starts():
    asyncio.run(
        _assert_realtime_phone_gateway_ignores_opening_barge_in_before_playback_starts()
    )


def test_realtime_phone_gateway_disables_local_opening_interrupt_when_barge_in_off():
    asyncio.run(_assert_realtime_phone_gateway_does_not_locally_interrupt_opening())


def test_realtime_phone_gateway_uses_opening_speaker_for_live_session():
    asyncio.run(_assert_realtime_phone_gateway_uses_opening_speaker())


def test_realtime_phone_gateway_seeds_opening_as_assistant_context():
    asyncio.run(_assert_realtime_phone_gateway_seeds_opening_context())


def test_realtime_instructions_do_not_reuse_historical_time_question():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.committed_exchanges.append(
        ConversationExchange(
            turn_id=1,
            input_transcript="现在几点？",
            output_transcript="现在是下午三点。",
        )
    )

    instructions = server._instructions_for_realtime_session(session)

    assert "不能当作本轮用户的新问题" in instructions
    assert "除非用户最新一句明确询问时间，否则不要主动报时" in instructions
    assert "如果打断后的最新语音不清楚" in instructions
    assert "现在几点？" not in instructions

    dialog_context = server._dialog_config_for_realtime_session(session).dialog_context

    assert [item.role for item in dialog_context] == ["user", "assistant"]
    assert [item.text for item in dialog_context] == [
        "现在几点？",
        "现在是下午三点。",
    ]


def test_realtime_phone_gateway_records_inbound_audio_rms_stats():
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )

    session.inbound_frames = 1
    _record_inbound_audio_rms(session, _phone_frame(100), threshold=300)
    session.inbound_frames = 2
    _record_inbound_audio_rms(session, _phone_frame(400), threshold=300)
    session.inbound_frames = 3
    _record_inbound_audio_rms(session, _phone_frame(0), threshold=300)

    assert session.inbound_rms_min == 0
    assert session.inbound_rms_max == 400
    assert session.inbound_rms_last == 0
    assert _inbound_rms_avg(session) == 167
    assert session.inbound_high_rms_frames == 1
    assert session.inbound_first_high_rms_frame == 2


def test_realtime_phone_gateway_skips_inbound_audio_rms_when_diagnostics_disabled():
    asyncio.run(_assert_realtime_phone_gateway_skips_inbound_audio_rms_when_disabled())


def test_handoff_request_detection_is_conservative():
    assert _detect_handoff_request("我要转人工") == "request_human"
    assert _detect_handoff_request("转工") == "request_human"
    assert _detect_handoff_request("帮我转工") == "request_human"
    assert _detect_handoff_request("我要转客服") == "request_human"
    assert _detect_handoff_request("接人工") == "request_human"
    assert _detect_handoff_request("麻烦帮我转接人工客服") == "request_human"
    assert _detect_handoff_request("我想找真人客服") == "request_human"
    assert _detect_handoff_request("我要找物业客服") == "request_human"
    assert _detect_handoff_request("让工作人员跟我说") == "request_human"
    assert _detect_handoff_request("我不想跟机器人说") == "request_human"
    assert _detect_handoff_request("不要机器人") == "request_human"
    assert _detect_handoff_request("不用转人工") is None
    assert _detect_handoff_request("不要转人工") is None
    assert _detect_handoff_request("先别找客服") is None
    assert _detect_handoff_request("我不是要转人工") is None
    assert _detect_handoff_request("你是机器人吗") is None
    assert _detect_handoff_request("你是人工客服吗") is None
    assert _detect_handoff_request("你是真人客服吗") is None
    assert _detect_handoff_request("叫负责人来") is None
    assert _detect_handoff_request("这个人工费是什么") is None
    assert _detect_handoff_request("我要人工费明细") is None
    assert _detect_handoff_request("人工智能能处理吗") is None


def test_agent_takeover_suggestion_detection_is_exact_for_first_version():
    detector = realtime_gateway._detect_agent_takeover_suggestion
    assert detector("我想投诉") == "complaint"
    assert detector("我想，投诉") == "complaint"
    assert detector(" 我 想 投 诉 ") == "complaint"
    assert detector("我想投诉你们物业") is None
    assert detector("我不是想投诉") is None
    assert detector("我要转人工") is None


def test_realtime_gateway_triggers_handoff_and_suppresses_model_output():
    asyncio.run(_assert_realtime_gateway_triggers_handoff_and_suppresses_model_output())


def test_realtime_gateway_triggers_handoff_from_asr_before_model_audio():
    asyncio.run(_assert_realtime_gateway_triggers_handoff_from_asr_before_model_audio())


def test_realtime_gateway_records_takeover_suggestion_without_handoff():
    asyncio.run(_assert_realtime_gateway_records_takeover_suggestion_without_handoff())


def test_realtime_gateway_defers_call_result_when_handoff_is_requested():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        call_result_writer=FakeCallResultWriter(),
    )
    session = RealtimePhoneSessionStats(
        call_id="customer-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.disconnected_at = 1
    session.handoff_requested = True
    session.committed_exchanges.append(
        ConversationExchange(
            turn_id=1,
            status="handoff_requested",
            input_transcript="我要转人工",
            source="handoff_requested",
        )
    )

    server._enqueue_call_result(session)

    assert server.call_result_writer.payloads == []


async def _assert_realtime_phone_gateway_skips_inbound_audio_rms_when_disabled():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(
            tail_silence_ms=0,
            inbound_rms_diagnostics_enabled=False,
        ),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=server.expected_frame_bytes,
    )

    await server._handle_audio_frame(session, _phone_frame(400))

    assert session.inbound_frames == 1
    assert session.inbound_rms_count == 0
    assert session.inbound_rms_max is None
    assert _inbound_rms_avg(session) is None


def test_realtime_phone_gateway_records_inbound_audio_rms_when_diagnostics_enabled():
    asyncio.run(_assert_realtime_phone_gateway_records_inbound_audio_rms_when_enabled())


async def _assert_realtime_phone_gateway_records_inbound_audio_rms_when_enabled():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(
            tail_silence_ms=0,
            inbound_rms_diagnostics_enabled=True,
        ),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=server.expected_frame_bytes,
    )

    await server._handle_audio_frame(session, _phone_frame(400))

    assert session.inbound_frames == 1
    assert session.inbound_rms_count == 1
    assert session.inbound_rms_max == 400
    assert session.inbound_high_rms_frames == 1


def test_call_result_payload_uses_committed_exchanges_as_authoritative_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=10.0,
        last_seen_at=10.0,
        expected_frame_bytes=320,
        recording_path="/var/lib/freeswitch/recordings/990000000000032001.wav",
        context={
            "callId": "990000000000032001",
            "debtId": "2049810626160668673",
            "identityName": "项目员工",
        },
        opening_text="您好，系统显示您还有物业费未缴。",
        opening_text_hash="hash-opening",
        opening_voice="female",
        opening_speaker="zh_female_vv_jupiter_bigtts",
    )
    session.disconnected_at = 12.0
    session.committed_exchanges.append(
        ConversationExchange(
            turn_id=1,
            status="interrupted",
            input_transcript="这个费用是什么？",
            output_transcript="这是三月份的物业费。",
            heard_output_transcript="这是三月份",
            played_audio_ms=820,
            playback_completed=False,
            source="client_interrupt",
            created_at_ms=1770000005000,
        )
    )
    session.committed_exchanges.append(
        ConversationExchange(
            turn_id=2,
            status="completed",
            input_transcript="我先核对一下金额。",
            output_transcript="",
            playback_completed=True,
            source="playback_completed",
        )
    )

    payload = server._build_call_result_payload(session)

    assert payload["recording_path"] == (
        "/var/lib/freeswitch/recordings/990000000000032001.wav"
    )
    assert payload["context"] == {
        "callId": "990000000000032001",
        "debtId": "2049810626160668673",
        "identityName": "项目员工",
    }
    assert "input_transcripts" not in payload
    assert "output_transcripts" not in payload
    assert payload["committed_exchanges"] == [
        {
            "turn_id": 1,
            "status": "interrupted",
            "question_id": None,
            "reply_id": None,
            "input_transcript": "这个费用是什么？",
            "output_transcript": "这是三月份的物业费。",
            "heard_output_transcript": "这是三月份",
            "played_audio_ms": 820,
            "playback_completed": False,
            "source": "client_interrupt",
            "created_at_ms": 1770000005000,
        },
        {
            "turn_id": 2,
            "status": "completed",
            "question_id": None,
            "reply_id": None,
            "input_transcript": "我先核对一下金额。",
            "output_transcript": "",
            "heard_output_transcript": "",
            "played_audio_ms": 0,
            "playback_completed": True,
            "source": "playback_completed",
            "created_at_ms": None,
        },
    ]
    assert payload["opening"]["text"] == "您好，系统显示您还有物业费未缴。"
    assert payload["turns"] == [
        {"role": "assistant", "text": "您好，系统显示您还有物业费未缴。"},
        {"role": "user", "text": "这个费用是什么？"},
        {"role": "assistant", "text": "这是三月份的物业费。"},
        {"role": "user", "text": "我先核对一下金额。"},
    ]
    assert payload["metrics"]["gateway_history_interrupted_turns"] == 1


def test_abandoned_pending_turn_is_committed_as_interrupted_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.pending_exchanges[3] = ConversationExchange(
        turn_id=3,
        input_transcript="这个费用是什么？",
        output_transcript="这是三月份的物业费。",
    )
    session.turn_first_playback_at[3] = 10.0
    session.turn_last_playback_at[3] = 10.82

    server._abandon_pending_turn(session, 3, reason="user_interrupt")

    assert session.pending_exchanges == {}
    assert len(session.committed_exchanges) == 1
    exchange = session.committed_exchanges[0]
    assert exchange.status == "interrupted"
    assert exchange.output_transcript == "这是三月份的物业费。"
    assert exchange.heard_output_transcript == ""
    assert exchange.playback_completed is False
    assert exchange.source == "client_interrupt"
    assert exchange.played_audio_ms == 820
    assert session.gateway_history_interrupted_turns == 1


async def _assert_realtime_gateway_triggers_handoff_and_suppresses_model_output():
    fake_handoff = FakeHandoffRequester()
    fake_playback_control = FakePlaybackControl()
    fake_realtime_session = FakeRealtimeSession(
        b"",
        auto_provider_events=False,
        restart_on_interruption=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0, handoff_wait_timeout_seconds=12),
        api_key="test-key",
        playback_control=fake_playback_control,
        handoff_requester=fake_handoff,
    )
    session = RealtimePhoneSessionStats(
        call_id="customer-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    server._realtime_sessions[session.session_id] = fake_realtime_session

    await server._queue_audio_delta(session, 4, samples_to_pcm_s16le([1600] * 480))
    assert not session.playback_queue.empty()

    await server._finalize_server_vad_turn(
        session,
        RealtimeTurnResult(
            turn_id=4,
            input_audio_bytes=640,
            output_audio_bytes=320,
            input_transcript="我要转人工",
            output_transcript="我帮您转接，请稍等。",
            event_counts={},
            first_audio_delta_ms=50,
            response_done_ms=120,
            status="completed",
        ),
    )

    assert fake_handoff.requests == [
        (
            "customer-call",
            {
                "trigger": "customer_requested",
                "reason": "request_human",
                "last_utterance": "我要转人工",
                "wait_timeout_seconds": 12,
                "ai_turns": [
                    {"role": "user", "text": "我要转人工"},
                    {"role": "assistant", "text": HANDOFF_CONNECTING_PROMPT_TEXT},
                ],
            },
        )
    ]
    assert session.handoff_requested is True
    assert session.handoff_completed is True
    assert session.handoff_error is None
    assert fake_realtime_session.tts_texts == [HANDOFF_CONNECTING_PROMPT_TEXT]
    assert session.current_output_turn_id is None
    assert session.playback_queue.empty()
    assert fake_playback_control.break_calls == ["customer-call"]
    assert fake_realtime_session.cancel_calls == 1
    assert fake_realtime_session.close_calls == 1
    committed = [
        (item.status, item.input_transcript, item.output_transcript)
        for item in session.committed_exchanges
    ]
    assert committed == [
        ("handoff_requested", "我要转人工", ""),
        ("completed", "", HANDOFF_CONNECTING_PROMPT_TEXT),
    ]
    assert session.context_repair_requests == 0

    await server._queue_audio_delta(session, 5, samples_to_pcm_s16le([1600] * 480))

    assert session.playback_queue.empty()
    assert session.dropped_stale_frames == 1
    assert len(fake_handoff.requests) == 1


async def _assert_realtime_gateway_triggers_handoff_from_asr_before_model_audio():
    fake_handoff = FakeHandoffRequester()
    fake_playback_control = FakePlaybackControl()
    fake_realtime_session = FakeRealtimeSession(
        b"",
        auto_provider_events=False,
        restart_on_interruption=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        playback_control=fake_playback_control,
        handoff_requester=fake_handoff,
    )
    session = RealtimePhoneSessionStats(
        call_id="customer-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    server._realtime_sessions[session.session_id] = fake_realtime_session

    await server._handle_input_transcript_available(session, 4, "接人工")
    await server._queue_audio_delta(session, 4, samples_to_pcm_s16le([1600] * 480))

    assert fake_handoff.requests == [
        (
            "customer-call",
            {
                "trigger": "customer_requested",
                "reason": "request_human",
                "last_utterance": "接人工",
                "wait_timeout_seconds": 180,
                "ai_turns": [
                    {"role": "user", "text": "接人工"},
                    {"role": "assistant", "text": HANDOFF_CONNECTING_PROMPT_TEXT},
                ],
            },
        )
    ]
    assert session.handoff_requested is True
    assert session.handoff_completed is True
    assert fake_realtime_session.tts_texts == [HANDOFF_CONNECTING_PROMPT_TEXT]
    assert session.playback_queue.empty()
    assert session.dropped_stale_frames == 1
    assert fake_playback_control.break_calls == ["customer-call"]
    assert fake_realtime_session.cancel_calls == 1
    assert fake_realtime_session.close_calls == 1
    committed = [
        (item.status, item.input_transcript, item.output_transcript)
        for item in session.committed_exchanges
    ]
    assert committed == [
        ("handoff_requested", "接人工", ""),
        ("completed", "", HANDOFF_CONNECTING_PROMPT_TEXT),
    ]


async def _assert_realtime_gateway_records_takeover_suggestion_without_handoff():
    fake_handoff = FakeHandoffRequester()
    fake_suggestion = FakeAgentTakeoverSuggestionRecorder()
    fake_playback_control = FakePlaybackControl()
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        playback_control=fake_playback_control,
        handoff_requester=fake_handoff,
        agent_takeover_suggestion_recorder=fake_suggestion,
    )
    session = RealtimePhoneSessionStats(
        call_id="customer-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_output_turn_id = 3

    await server._handle_input_transcript_available(session, 4, "我想投诉")

    assert fake_suggestion.requests == [
        (
            "customer-call",
            {
                "reason": "complaint",
                "last_utterance": "我想投诉",
            },
        )
    ]
    assert fake_handoff.requests == []
    assert session.handoff_requested is False
    assert session.agent_takeover_suggestion_requested is True
    assert session.current_output_turn_id == 3
    assert fake_playback_control.break_calls == []


def test_realtime_gateway_drops_late_audio_for_closed_interrupted_turn():
    asyncio.run(_assert_realtime_gateway_drops_late_audio_for_closed_interrupted_turn())


async def _assert_realtime_gateway_drops_late_audio_for_closed_interrupted_turn():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.closed_output_turn_ids.add(3)

    await server._queue_audio_delta(session, 3, samples_to_pcm_s16le([1000] * 480))

    assert session.playback_queue.empty()
    assert session.dropped_stale_frames == 1


def test_cancelled_closed_turn_is_committed_as_interrupted_history():
    asyncio.run(_assert_cancelled_closed_turn_is_committed_as_interrupted_history())


async def _assert_cancelled_closed_turn_is_committed_as_interrupted_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.closed_output_turn_ids.add(3)

    await server._finalize_server_vad_turn(
        session,
        RealtimeTurnResult(
            turn_id=3,
            input_audio_bytes=640,
            output_audio_bytes=320,
            input_transcript="这个费用是什么？",
            output_transcript="这是三月份的物业费。",
            event_counts={},
            first_audio_delta_ms=50,
            response_done_ms=120,
            status="cancelled",
        ),
    )

    assert session.pending_exchanges == {}
    assert len(session.committed_exchanges) == 1
    exchange = session.committed_exchanges[0]
    assert exchange.status == "interrupted"
    assert exchange.input_transcript == "这个费用是什么？"
    assert exchange.output_transcript == "这是三月份的物业费。"
    assert exchange.playback_completed is False
    assert session.gateway_history_interrupted_turns == 1


def test_delayed_cancelled_turn_transcript_does_not_double_count_abandoned_history():
    asyncio.run(
        _assert_delayed_cancelled_turn_transcript_does_not_double_count_abandoned_history()
    )


async def _assert_delayed_cancelled_turn_transcript_does_not_double_count_abandoned_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )

    server._abandon_pending_turn(session, 3, reason="user_interrupt")

    await server._finalize_server_vad_turn(
        session,
        RealtimeTurnResult(
            turn_id=3,
            input_audio_bytes=640,
            output_audio_bytes=320,
            input_transcript="这个费用是什么？",
            output_transcript="这是三月份的物业费。",
            event_counts={},
            first_audio_delta_ms=50,
            response_done_ms=120,
            status="cancelled",
        ),
    )

    assert session.gateway_history_abandoned_turns == 0
    assert session.gateway_history_interrupted_turns == 1
    assert [exchange.status for exchange in session.committed_exchanges] == [
        "interrupted"
    ]


def test_abandoned_opening_turn_is_not_committed_as_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.output_transcripts_by_turn[OPENING_TURN_ID] = "您好，我是物业中心小明。"

    server._abandon_pending_turn(session, OPENING_TURN_ID, reason="user_interrupt")

    assert session.committed_exchanges == []
    assert session.gateway_history_interrupted_turns == 0


def test_realtime_instructions_anchor_opening_confirmation_to_fee_followup():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        opening_text="您好，请问是测试业主吗？系统显示您当前有12.34元待缴费用，想和您确认一下。",
        opening_text_hash="hash-opening",
        opening_voice="male",
    )

    instructions = server._instructions_for_realtime_session(session)

    assert "待缴费用确认电话" in instructions
    assert "只有用户明确说自己是业主本人、授权处理人，或明确表示自己可以处理该费用事项，才视为身份已确认" in instructions
    assert "用户只说“方便”“可以”“好的”“嗯”“对”“是的”“在的”“你说吧”等短句时，不能视为已确认身份" in instructions
    assert "无论是否确认身份，都不得在通话中说出具体金额" in instructions
    assert "未确认身份前不得披露地址、房号或费用明细" in instructions
    assert "12.34元" not in instructions
    assert "[金额已隐藏]" in instructions
    assert "如果用户最新一句是在确认身份，例如" not in instructions
    assert "例如“是的”“对”“嗯”“我是”“在的”" not in instructions
    assert "数据库催收策略决定业务目标、推进方向和可表达的信息范围" in instructions
    assert "客服语气配置决定表达风格、正式程度和语气强弱" in instructions
    assert "不得让开场白反向覆盖数据库策略" in instructions
    assert "不得因策略阶段升级而忽略客服语气配置" in instructions
    assert "以已播放开场白为语气参照" not in instructions
    assert "保持相同的身份、称呼方式、语气基调和沟通边界" in instructions
    assert "不要突然变得更强硬、更随意" in instructions
    assert "全程使用“您”" in instructions
    assert "不要说“你家”" in instructions
    assert "避免使用“尽快缴纳”“不影响物业服务”" in instructions
    assert "要求勿扰后必须礼貌结束" in instructions
    assert "不得承诺回拨或约定回拨时间" in instructions
    assert "不得再询问付款时间、缴费计划或租客联系方式" in instructions
    assert "即使用户主动提到租客" in instructions
    assert "用户只说没钱" in instructions
    assert "不得主动询问发薪日" in instructions
    assert "不得列举前台、公告栏、单元门口" in instructions
    assert "用户提到起诉、法院、律师、征信或上门时" in instructions
    assert "不得评价起诉是否为合法权利" in instructions
    assert "不得说暂未涉及征信" in instructions
    assert "不得说为避免不必要的麻烦" in instructions
    assert "不得使用尽快处理" in instructions
    assert "不得使用正式催告、法律跟进阶段、可能面临诉讼等法律施压表达" in instructions
    assert "不得编造X日、X日承诺缴纳、未实际到账等系统未明确提供的事实" in instructions
    assert "用户提到起诉、法院、律师、征信或上门时，本轮回复只能中性收口" in instructions
    assert "用户已明确拒缴后" in instructions
    assert "回答发票、渠道、明细、征信、起诉等直接问题后必须继续收口" in instructions
    assert "部分缴纳或费用减免" in instructions
    assert "不得脱离本轮业务策略自行承诺" in instructions
    assert "本轮业务策略未给出明确方案时只能记录意向" in instructions
    assert "不得说可以的、交多少都行" in instructions
    assert "用户提出电梯、维修、卫生、服务质量等投诉后" in instructions
    assert "不得在同一回复里继续催缴" in instructions
    assert "不得说还得麻烦您尽快处理" in instructions
    assert "用户已明确拒缴或拒绝联系后，只允许收口一次" in instructions
    assert "不得反复附带后续若想处理" in instructions
    assert "记录反馈时只说记录您的反馈或诉求" in instructions
    assert "不得说记录您的态度" in instructions
    assert "用户否认本人后" in instructions
    assert "不得再次要求身份确认" in instructions
    assert "用户抱怨啰嗦、要求直接说、追问什么事但仍未确认身份时" in instructions
    assert "严禁主动切换到化妆" in instructions


def test_realtime_dialog_config_uses_committed_history_as_dialog_context():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        prompt_snapshot=PromptSnapshot(
            scene="fee",
            version="v1",
            instructions="完整业务提示词",
            metadata={"employee_name": "小明"},
            content_hash="hash-a",
            loaded_at_ms=123,
        ),
    )
    session.committed_exchanges.extend(
        [
            ConversationExchange(
                turn_id=1,
                status="completed",
                input_transcript="你是哪边？",
                output_transcript="我是物业中心小明。",
            ),
            ConversationExchange(
                turn_id=2,
                status="interrupted",
                input_transcript="我刚才说的5200.75元是什么？",
                output_transcript="具体是5200.75元的物业费。",
            ),
        ]
    )

    dialog_config = server._dialog_config_for_realtime_session(session)

    assert [item.role for item in dialog_config.dialog_context] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [item.text for item in dialog_config.dialog_context] == [
        "你是哪边？",
        "我是物业中心小明。",
        "我刚才说的[金额已隐藏]是什么？",
        "具体是[金额已隐藏]的物业费。",
    ]


def test_realtime_dialog_context_skips_oversized_older_exchange():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.committed_exchanges.extend(
        [
            ConversationExchange(
                turn_id=1,
                input_transcript="旧问题",
                output_transcript="旧回答" * 1000,
            ),
            ConversationExchange(
                turn_id=2,
                input_transcript="新问题",
                output_transcript="新回答",
            ),
        ]
    )

    dialog_context = server._dialog_context_for_realtime_session(session)

    assert [item.text for item in dialog_context] == ["新问题", "新回答"]


def test_realtime_dialog_config_anchors_postgres_employee_identity():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        prompt_snapshot=PromptSnapshot(
            scene="项目员工:7",
            version="postgres",
            instructions="完整业务提示词\n先确认本人，再说明待缴物业费。",
            content_hash="hash-prompt",
            loaded_at_ms=123,
            metadata={
                "source": "postgres",
                "identityName": "项目员工",
                "employee_name": "物业中心小明",
                "strategy_core": "先确认本人，再说明待缴物业费。",
                "speaking_style": "协调型、熟人式、耐心沟通的物业工作人员口吻。",
            },
        ),
    )

    dialog_config = server._dialog_config_for_realtime_session(session)

    assert dialog_config == RealtimeDialogConfig(
        bot_name="物业中心小明",
        system_role=dialog_config.system_role,
        speaking_style=dialog_config.speaking_style,
        model="1.2.1.1",
    )
    assert "物业中心小明" in dialog_config.system_role
    assert "禁止自称豆包" in dialog_config.system_role
    assert "小区物业项目员工" in dialog_config.system_role
    assert "先确认本人，再说明待缴物业费。" in dialog_config.system_role
    assert "完整业务提示词" in dialog_config.system_role
    assert "不能当作本轮用户的新问题" in dialog_config.system_role
    assert dialog_config.speaking_style == "协调型、熟人式、耐心沟通的物业工作人员口吻。"


def test_realtime_dialog_config_treats_lawyer_identity_as_legal_contact():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        prompt_snapshot=PromptSnapshot(
            scene="律师:7",
            version="postgres",
            instructions="律师阶段业务提示词",
            content_hash="hash-prompt",
            loaded_at_ms=123,
            metadata={
                "source": "postgres",
                "identityName": "律师",
                "employee_name": "律师赵敏",
            },
        ),
    )

    dialog_config = server._dialog_config_for_realtime_session(session)

    assert "律师赵敏" in dialog_config.system_role
    assert "受物业公司委托的法律事务联系人" in dialog_config.system_role


def test_realtime_dialog_config_warns_when_dialog_prompt_is_too_long(caplog):
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    long_business_prompt = "业务规则" * DIALOG_PROMPT_SOFT_LIMIT_CHARS
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        prompt_snapshot=PromptSnapshot(
            scene="项目员工:7",
            version="postgres",
            instructions=long_business_prompt,
            content_hash="hash-prompt",
            loaded_at_ms=123,
            metadata={
                "source": "postgres",
                "identityName": "项目员工",
                "employee_name": "物业中心小明",
                "speaking_style": "协调型、熟人式、耐心沟通的物业工作人员口吻。",
            },
        ),
    )

    with caplog.at_level("WARNING"):
        dialog_config = server._dialog_config_for_realtime_session(session)

    assert long_business_prompt in dialog_config.system_role
    assert "dialog_prompt_soft_limit_exceeded" in caplog.text
    assert "system_role_chars=" in caplog.text
    assert "speaking_style_chars=" in caplog.text
    assert "soft_limit_chars=" in caplog.text
    assert long_business_prompt not in caplog.text


def test_realtime_gateway_passes_dialog_config_to_realtime_factory():
    captured: dict[str, RealtimeDialogConfig] = {}

    def session_factory(
        on_speech_started,
        on_input_transcript,
        on_delta,
        on_turn_completed,
        turn_id_start,
        instructions,
        speaker,
        dialog_config,
    ):
        captured["dialog_config"] = dialog_config
        return FakeRealtimeSession(b"").bind(
            on_speech_started,
            on_input_transcript,
            on_delta,
            on_turn_completed,
            turn_id_start,
            instructions,
            speaker,
            dialog_config,
        )

    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=session_factory,
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        prompt_snapshot=PromptSnapshot(
            scene="项目员工:7",
            version="postgres",
            instructions="完整业务提示词",
            content_hash="hash-prompt",
            loaded_at_ms=123,
            metadata={"source": "postgres", "employee_name": "物业中心小明"},
        ),
    )

    server._create_realtime_session(session)

    assert captured["dialog_config"].bot_name == "物业中心小明"
    assert "完整业务提示词" in captured["dialog_config"].system_role


def test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id():
    asyncio.run(_assert_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id())


async def _assert_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id() -> None:
    snapshot = PromptSnapshot(
        scene="collector-a:persona-1",
        version="postgres",
        instructions="业务提示词",
        content_hash="hash-prompt",
        loaded_at_ms=123,
        metadata={"source": "postgres"},
    )

    class FailingStore:
        async def get_prompt_snapshot(self, scene=None, *, fallback_instructions=None):
            raise AssertionError("legacy prompt store should not be queried")

    server = FreeSwitchRealtimeGatewayServer(
        GatewayConfig(),
        api_key="test",
        prompt_store=FailingStore(),
        prompt_snapshot_provider=lambda call_id: snapshot if call_id == "call-1" else None,
    )
    session = RealtimePhoneSessionStats(
        call_id="call-1",
        session_id="session-1",
        connected_at=1.0,
        last_seen_at=1.0,
        expected_frame_bytes=320,
    )

    loaded = await server._load_prompt_snapshot(session)

    assert loaded is snapshot


async def _assert_realtime_phone_gateway_roundtrip() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-realtime-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)
            assert len(playback) == 320
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert fake_session.connected is True
    assert fake_session.closed is True
    assert fake_session.appended_bytes == 1920
    assert fake_session.speech_started_turns == [1]
    assert len(server.completed_sessions) == 1
    stats = server.completed_sessions[0]
    assert stats.turns_started == 1
    assert stats.turns_committed == 1
    assert stats.turns_completed == 1
    assert stats.turns_failed == 0
    assert stats.outbound_frames == 1
    assert stats.flushed_tail_frames == 1
    payload = server._build_call_result_payload(stats)
    assert "output_transcripts" not in payload
    assert stats.gateway_history_committed_turns == 1
    assert stats.gateway_history_abandoned_turns == 0
    assert [item.output_transcript for item in stats.committed_exchanges] == [
        "hello from model"
    ]


async def _assert_realtime_phone_gateway_interrupts_playback() -> None:
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 480 * 20),
        reconnect_delay_seconds=0.05,
        restart_on_interruption=False,
    )
    fake_playback_control = FakePlaybackControl()
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=fake_playback_control,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-interrupt-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)

            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.interruptions == 1
    assert stats.dropped_playback_frames > 0
    assert stats.dropped_stale_frames == 0
    assert stats.freeswitch_break_requests == 1
    assert stats.freeswitch_break_failures == 0
    assert stats.realtime_interrupt_requests == 1
    assert stats.realtime_interrupt_failures == 0
    assert stats.context_repair_requests == 1
    assert stats.realtime_session_restarts == 0
    assert stats.gateway_history_committed_turns == 1
    assert stats.gateway_history_abandoned_turns == 0
    assert stats.replayed_input_frames == 0
    interrupted = [
        exchange
        for exchange in stats.committed_exchanges
        if exchange.status == "interrupted"
    ]
    assert interrupted
    assert interrupted[0].output_transcript == "hello from model"
    assert interrupted[0].playback_completed is False
    assert fake_playback_control.break_calls == ["test-interrupt-call"]
    assert fake_session.cancel_calls == 1
    assert fake_session.interruption_calls == ["hello from model"]
    assert fake_session.connect_calls == 1
    assert fake_session.turn_id_starts == [0]
    assert fake_session.close_calls == 1
    assert all(size <= 640 for size in fake_session.append_sizes)


async def _assert_realtime_phone_gateway_interrupts_pending_provider_turn() -> None:
    fake_session = FakeRealtimeSession(
        b"",
        restart_on_interruption=False,
        auto_provider_events=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    session = RealtimePhoneSessionStats(
        call_id="test-pending-provider-call",
        session_id="test-pending-provider-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_capture_turn_id = 4
    session.turn_speech_started_at[4] = time.monotonic()
    session.recent_input_frames_16k.append(b"\x01\x00" * 320)
    server._realtime_sessions[session.session_id] = fake_session

    await server._handle_server_vad_speech_started(session, 5)
    await _wait_until(lambda: not session.interruption_repair_active)

    assert 5 in session.turn_speech_started_at
    assert session.current_capture_turn_id == 5
    assert session.turns_started == 1
    assert session.interruptions == 1
    assert session.context_repair_requests == 1
    assert fake_session.interruption_calls == [None]
    assert fake_session.cancel_calls == 1


async def _assert_realtime_phone_gateway_restart_clears_interrupted_capture() -> None:
    fake_session = FakeRealtimeSession(
        b"",
        restart_on_interruption=True,
        auto_provider_events=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    session = RealtimePhoneSessionStats(
        call_id="test-restart-capture-call",
        session_id="test-restart-capture-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_capture_turn_id = 5
    session.repair_replay_frames_16k.append(b"\x01\x00" * 320)
    server._realtime_sessions[session.session_id] = fake_session

    await server._restart_realtime_session_after_interruption(
        session,
        fake_session,
        reason="server_vad_speech_started",
    )

    assert session.current_capture_turn_id is None
    assert session.replayed_input_frames == 1
    assert fake_session.connect_calls == 1
    assert fake_session.append_sizes == [640]


async def _assert_realtime_phone_gateway_replays_after_fallback_hot_restart() -> None:
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 480 * 20),
        restart_on_interruption=True,
    )
    fake_playback_control = FakePlaybackControl()
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=fake_playback_control,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-hot-restart-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)

            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.interruptions == 1
    assert stats.realtime_session_restarts == 1
    assert stats.replayed_input_frames > 0
    assert stats.replayed_input_bytes > 0
    assert fake_session.interruption_calls == []
    assert fake_session.connect_calls == 2
    assert fake_session.close_calls >= 1
    assert any(size > 640 for size in fake_session.append_sizes)


async def _assert_realtime_phone_gateway_does_not_replay_when_context_repair_fails() -> None:
    fake_session = FakeRealtimeSession(
        b"",
        restart_on_interruption=False,
        auto_provider_events=False,
        interruption_error=TimeoutError("context repair timed out"),
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    session = RealtimePhoneSessionStats(
        call_id="test-repair-failure-call",
        session_id="test-repair-failure-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    opening_text = "您好，请问是测试业主吗？"
    session.output_transcripts_by_turn[0] = opening_text
    session.repair_replay_frames_16k.append(b"\x01\x00" * 320)
    session.repair_replay_frames_16k.append(b"\x02\x00" * 320)
    server._realtime_sessions[session.session_id] = fake_session

    await server._interrupt_realtime_playback_context(
        session,
        fake_session,
        reason="local_opening_barge_in",
        interrupted_output_turn_id=0,
    )

    assert fake_session.interruption_calls == [opening_text]
    assert session.replayed_input_frames == 0
    assert session.replayed_input_bytes == 0
    assert fake_session.append_sizes == []
    assert list(session.repair_replay_frames_16k) == []
    assert session.realtime_interrupt_failures == 1


async def _assert_realtime_phone_gateway_waits_for_slow_context_repair() -> None:
    fake_session = FakeRealtimeSession(
        b"",
        restart_on_interruption=False,
        auto_provider_events=False,
        interruption_delay_seconds=2.05,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    session = RealtimePhoneSessionStats(
        call_id="test-slow-repair-call",
        session_id="test-slow-repair-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    opening_text = "您好，请问是测试业主吗？"
    session.output_transcripts_by_turn[0] = opening_text
    session.repair_replay_frames_16k.append(b"\x01\x00" * 320)
    server._realtime_sessions[session.session_id] = fake_session

    await server._interrupt_realtime_playback_context(
        session,
        fake_session,
        reason="local_opening_barge_in",
        interrupted_output_turn_id=0,
    )

    assert fake_session.interruption_calls == [opening_text]
    assert session.replayed_input_frames == 0
    assert fake_session.append_sizes == []
    assert list(session.repair_replay_frames_16k) == []
    assert session.realtime_interrupt_failures == 0
    assert session.context_repair_requests == 1


async def _assert_realtime_phone_gateway_appends_tail_silence() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=40),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-tail-silence-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback_frames = [
                await asyncio.wait_for(ws.recv(), timeout=3) for _ in range(3)
            ]
            assert all(isinstance(frame, bytes) for frame in playback_frames)
            assert playback_frames[1:] == [b"\x00" * 320, b"\x00" * 320]
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.outbound_frames == 3
    assert stats.flushed_tail_frames == 1
    assert stats.tail_silence_frames == 2
    assert stats.gateway_history_committed_turns == 1


async def _assert_realtime_phone_gateway_waits_for_freeswitch_completion() -> None:
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=FakePlaybackControl(),
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-playback-complete-call",
            ping_interval=None,
        ) as ws:
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(0))
            await ws.send(_phone_frame(0))

            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert isinstance(playback, bytes)
            await asyncio.sleep(0.05)

            session = next(iter(server.active_sessions.values()))
            assert session.gateway_history_committed_turns == 0

            await server._handle_freeswitch_playback_event(
                PlaybackProgressEvent(
                    uuid="test-playback-complete-call",
                    event="queue_completed",
                    total_chunks=1,
                )
            )
            assert session.gateway_history_committed_turns == 1
    finally:
        await server.stop()


async def _assert_realtime_phone_gateway_does_not_emit_silence_on_lag() -> None:
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0, send_interval_ms=10),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_output_turn_id = 1
    websocket = RecordingWebSocket()

    await server._send_playback_frame(
        websocket,
        session,
        PlaybackFrame(1, b"\x01" * 320),
    )

    assert websocket.sent == [b"\x01" * 320]
    assert session.playback_underruns == 1
    assert session.playback_realtime_send_frames == 1
    assert session.playback_fast_send_frames == 0
    assert session.playback_queue.empty()


async def _assert_realtime_phone_gateway_prefills_completed_opening() -> None:
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-opening-prefill-call",
        session_id="test-opening-prefill-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_output_turn_id = OPENING_TURN_ID
    session.model_done_turns.add(OPENING_TURN_ID)
    first = PlaybackFrame(OPENING_TURN_ID, _phone_frame(100))
    queued_frames = server.playback_prefill_frames + 3

    for index in range(queued_frames):
        await session.playback_queue.put(
            PlaybackFrame(OPENING_TURN_ID, _phone_frame(index + 101))
        )

    frames = await server._prefill_playback_frames(session, first)

    assert len(frames) == server.playback_prefill_frames
    assert frames[0].payload == _phone_frame(100)
    assert frames[-1].payload == _phone_frame(100 + server.playback_prefill_frames - 1)
    assert session.playback_queue.qsize() == queued_frames - (
        server.playback_prefill_frames - 1
    )
    assert OPENING_TURN_ID in session.jitter_prefilled_turns


async def _assert_realtime_phone_gateway_plays_opening_audio() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-call",
            opening_text=opening_text,
            opening_text_hash="hash-a",
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            phone_frames=[_phone_frame(800)],
            source_sample_rate=24000,
            source_audio_bytes=960,
            generation_ms=1200,
        )
    )
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_text_hash == "hash-a"
    assert stats.opening_playback_frames == 1
    assert stats.opening_playback_completed_at is not None
    assert stats.opening_playback_interrupted is False
    assert opening_text in fake_session.instructions[0]
    assert store.pop("test-opening-call") is None


async def _assert_realtime_phone_gateway_marks_failed_on_connect_error() -> None:
    writer = FakeCallResultWriter()
    fake_session = FakeRealtimeSession(
        b"",
        connect_error=RuntimeError("auth denied"),
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        call_result_writer=writer,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/connect-fail-call",
            ping_interval=None,
        ) as ws:
            with pytest.raises(Exception, match="auth denied|1011"):
                await asyncio.wait_for(ws.recv(), timeout=1)
        await _wait_until(lambda: len(writer.payloads) == 1)
    finally:
        await server.stop()

    payload = writer.payloads[0]
    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "realtime_session_connect_failed"
    assert "auth denied" in payload["error"]
    assert payload["metrics"]["outbound_frames"] == 0


async def _assert_realtime_phone_gateway_writes_opening_source_wav(tmp_path) -> None:
    host_dir = tmp_path / "recordings"
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(
            tail_silence_ms=0,
            call_recording=CallRecordingConfig(
                enabled=True,
                directory="/var/lib/freeswitch/recordings",
                host_directory=str(host_dir),
                opening_source_debug_enabled=True,
                opening_warmup_ms=0,
            ),
        ),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="media-call-1",
        session_id="session-1",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        recording_path="/var/lib/freeswitch/recordings/business-call-1.wav",
    )
    opening_audio = PreparedOpeningAudio(
        call_id="media-call-1",
        opening_text="您好，请问是测试业主吗？",
        opening_text_hash="hash-source-wav",
        voice="female",
        speaker="zh_female_vv_jupiter_bigtts",
        phone_frames=[_phone_frame(800), _phone_frame(1200)],
        source_sample_rate=24000,
        source_audio_bytes=960,
        generation_ms=1200,
    )

    await server._start_opening_playback(session, opening_audio)

    source_path = host_dir / "business-call-1.opening-source.wav"
    assert source_path.is_file()
    with wave.open(str(source_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 8000
        assert wav_file.readframes(320) == b"".join(opening_audio.phone_frames)


async def _assert_realtime_phone_gateway_adds_recording_opening_warmup() -> None:
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(
            tail_silence_ms=0,
            call_recording=CallRecordingConfig(
                enabled=True,
                directory="/var/lib/freeswitch/recordings",
                opening_warmup_ms=40,
            ),
        ),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="media-call-1",
        session_id="session-1",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        recording_path="/var/lib/freeswitch/recordings/business-call-1.wav",
    )
    opening_frame = _phone_frame(800)
    opening_audio = PreparedOpeningAudio(
        call_id="media-call-1",
        opening_text="您好，请问是测试业主吗？",
        opening_text_hash="hash-warmup",
        voice="female",
        speaker="zh_female_vv_jupiter_bigtts",
        phone_frames=[opening_frame],
        source_sample_rate=24000,
        source_audio_bytes=960,
        generation_ms=1200,
    )

    await server._start_opening_playback(session, opening_audio)

    frames = [
        session.playback_queue.get_nowait(),
        session.playback_queue.get_nowait(),
        session.playback_queue.get_nowait(),
    ]
    assert [frame.payload for frame in frames] == [
        b"\x00" * 320,
        b"\x00" * 320,
        opening_frame,
    ]
    assert session.opening_playback_frames == 1


def test_realtime_phone_gateway_skips_opening_audio_with_amount():
    asyncio.run(_assert_realtime_phone_gateway_skips_opening_audio_with_amount())


async def _assert_realtime_phone_gateway_skips_opening_audio_with_amount() -> None:
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-sensitive-opening-call",
            opening_text="您好，系统显示您当前有12.34元待缴费用。",
            opening_text_hash="hash-sensitive",
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            phone_frames=[_phone_frame(800)],
            source_sample_rate=24000,
            source_audio_bytes=960,
            generation_ms=1200,
        )
    )
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-sensitive-opening-call",
            ping_interval=None,
        ) as ws:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_text_hash is None
    assert stats.opening_playback_frames == 0
    assert fake_session.instructions
    assert "12.34元" not in fake_session.instructions[0]
    assert store.pop("test-sensitive-opening-call") is None


async def _assert_realtime_phone_gateway_waits_for_answer_before_opening_audio() -> None:
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-answer-gate-call",
            opening_text="您好，请问是测试业主吗？",
            opening_text_hash="hash-answer-gate",
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            phone_frames=[_phone_frame(800)],
            source_sample_rate=24000,
            source_audio_bytes=960,
            generation_ms=1200,
        )
    )
    answered = False

    def is_call_answered(call_id: str) -> bool:
        assert call_id == "test-opening-answer-gate-call"
        return answered

    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        opening_store=store,
        is_call_answered=is_call_answered,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-answer-gate-call",
            ping_interval=None,
        ) as ws:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.15)

            answered = True
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_text_hash == "hash-answer-gate"
    assert stats.opening_playback_frames == 1
    assert stats.opening_playback_completed_at is not None


async def _assert_realtime_phone_gateway_interrupts_opening_audio() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-interrupt-call",
            opening_text=opening_text,
            opening_text_hash="hash-b",
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            phone_frames=[_phone_frame(800) for _ in range(20)],
            source_sample_rate=24000,
            source_audio_bytes=6400,
            generation_ms=1200,
        )
    )
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 240),
        restart_on_interruption=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=FakePlaybackControl(),
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-interrupt-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await ws.send(_phone_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_text_hash == "hash-b"
    assert stats.opening_playback_interrupted is True
    assert fake_session.interruption_calls == [opening_text]


async def _assert_realtime_phone_gateway_locally_interrupts_opening() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-local-barge-call",
            opening_text=opening_text,
            opening_text_hash="hash-local-barge",
            voice="male",
            speaker="zh_male_yunzhou_jupiter_bigtts",
            phone_frames=[_phone_frame(800) for _ in range(20)],
            source_sample_rate=24000,
            source_audio_bytes=6400,
            generation_ms=1200,
        )
    )
    playback_control = FakePlaybackControl()
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 240),
        auto_provider_events=False,
        restart_on_interruption=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=playback_control,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-local-barge-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await asyncio.sleep(0.35)
            await ws.send(_speech_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_playback_interrupted is True
    assert stats.interruptions == 1
    assert stats.opening_trigger_rms == 1200
    assert stats.opening_trigger_rms_max == 1200
    assert stats.opening_trigger_best_playback_correlation < 0.4
    assert stats.opening_trigger_best_playback_rms == 800
    assert stats.opening_trigger_last_playback_age_ms is not None
    assert stats.opening_trigger_last_playback_age_ms >= 0
    assert playback_control.break_calls == ["test-opening-local-barge-call"]
    assert fake_session.speech_started_turns == []
    assert fake_session.interruption_calls == [opening_text]


async def _assert_realtime_phone_gateway_ignores_opening_playback_echo() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-echo-call",
            opening_text=opening_text,
            opening_text_hash="hash-opening-echo",
            voice="male",
            speaker="zh_male_yunzhou_jupiter_bigtts",
            phone_frames=[_phone_frame(800) for _ in range(20)],
            source_sample_rate=24000,
            source_audio_bytes=6400,
            generation_ms=1200,
        )
    )
    playback_control = FakePlaybackControl()
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 240),
        auto_provider_events=False,
        restart_on_interruption=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=playback_control,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-echo-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await asyncio.sleep(0.35)
            await ws.send(_phone_frame(800))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_playback_interrupted is False
    assert stats.interruptions == 0
    assert playback_control.break_calls == []
    assert fake_session.interruption_calls == []


async def _assert_realtime_phone_gateway_ignores_opening_barge_in_before_playback_starts() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-opening-unarmed-barge-call",
        session_id="test-opening-unarmed-barge-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        opening_text=opening_text,
        opening_text_hash="hash-local-barge-unarmed",
        opening_voice="female",
        opening_speaker="zh_female_vv_jupiter_bigtts",
    )
    session.current_output_turn_id = 0
    session.opening_playback_started_at = 0
    session.opening_playback_frames = 426
    session.opening_barge_in_detector = server._create_opening_barge_in_detector()

    async def noop_repair(
        repair_session: RealtimePhoneSessionStats,
        *,
        reason: str,
    ) -> None:
        del repair_session, reason

    server._run_interruption_repair = noop_repair  # type: ignore[method-assign]

    handled = server._handle_local_opening_barge_in(session, _phone_frame(1200))
    await asyncio.sleep(0)

    assert handled is False
    assert session.opening_playback_sent_frames == 0
    assert session.opening_playback_interrupted is False
    assert session.local_barge_in_events == 0
    assert session.interruptions == 0
    assert session.opening_trigger_rms is None
    assert list(session.opening_inbound_rms_values) == []


async def _assert_realtime_phone_gateway_does_not_locally_interrupt_opening() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-local-barge-disabled-call",
            opening_text=opening_text,
            opening_text_hash="hash-local-barge-disabled",
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            phone_frames=[_phone_frame(800) for _ in range(20)],
            source_sample_rate=24000,
            source_audio_bytes=6400,
            generation_ms=1200,
        )
    )
    playback_control = FakePlaybackControl()
    fake_session = FakeRealtimeSession(
        samples_to_pcm_s16le([1600] * 240),
        auto_provider_events=False,
        restart_on_interruption=False,
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0, barge_in_enabled=False),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        playback_control=playback_control,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-local-barge-disabled-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await ws.send(_phone_frame(1200))
            await asyncio.sleep(0.1)
    finally:
        await server.stop()

    stats = server.completed_sessions[0]
    assert stats.opening_playback_interrupted is False
    assert stats.local_barge_in_events == 0
    assert stats.interruptions == 0
    assert playback_control.break_calls == []
    assert fake_session.speech_started_turns == []
    assert fake_session.interruption_calls == []


async def _assert_realtime_phone_gateway_uses_opening_speaker() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    opening_speaker = "zh_male_yunzhou_jupiter_bigtts"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-speaker-call",
            opening_text=opening_text,
            opening_text_hash="hash-c",
            voice="male",
            speaker=opening_speaker,
            phone_frames=[_phone_frame(800)],
            source_sample_rate=24000,
            source_audio_bytes=960,
            generation_ms=1200,
        )
    )
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    captured_speakers: list[str | None] = []

    def session_factory(
        on_speech_started,
        on_input_transcript,
        on_delta,
        on_turn_completed,
        turn_id_start,
        instructions,
        *extra,
    ):
        captured_speakers.append(extra[0] if extra else None)
        return fake_session.bind(
            on_speech_started,
            on_input_transcript,
            on_delta,
            on_turn_completed,
            turn_id_start,
            instructions,
        )

    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=session_factory,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-speaker-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert captured_speakers == [opening_speaker]


async def _assert_realtime_phone_gateway_seeds_opening_context() -> None:
    opening_text = "您好，请问是测试业主吗？这边有一项物业费事项想和您本人核实一下。"
    store = OpeningAudioStore()
    store.put(
        PreparedOpeningAudio(
            call_id="test-opening-context-seed-call",
            opening_text=opening_text,
            opening_text_hash="hash-context-seed",
            voice="female",
            speaker="zh_female_vv_jupiter_bigtts",
            phone_frames=[_phone_frame(800)],
            source_sample_rate=24000,
            source_audio_bytes=960,
            generation_ms=1200,
        )
    )
    fake_session = FakeRealtimeSession(samples_to_pcm_s16le([1600] * 240))
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        realtime_session_factory=fake_session.bind,
        opening_store=store,
    )
    await server.start()
    try:
        host, port = server.address
        async with connect(
            f"ws://{host}:{port}/media/fs/test-opening-context-seed-call",
            ping_interval=None,
        ) as ws:
            playback = await asyncio.wait_for(ws.recv(), timeout=3)
            assert playback == _phone_frame(800)
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert fake_session.seed_context_calls == [opening_text]


class FakePlaybackControl:
    def __init__(self) -> None:
        self.break_calls: list[str] = []

    async def break_playback(self, media_uuid: str) -> bool:
        self.break_calls.append(media_uuid)
        return True


class FakeHandoffRequester:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    def __call__(self, call_id: str, payload: dict[str, object]) -> dict:
        self.requests.append((call_id, dict(payload)))
        return {
            "status": "accepted",
            "call": {
                "status": "waiting_agent",
                "handoff": {"state": "waiting_agent"},
            },
        }


class FakeAgentTakeoverSuggestionRecorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, call_id: str, payload: dict) -> dict:
        self.requests.append((call_id, dict(payload)))
        return {
            "status": "accepted",
            "call": {
                "agent_takeover_suggestion": {
                    "state": "suggested",
                    "reason": payload.get("reason"),
                    "last_utterance": payload.get("last_utterance"),
                    "can_takeover": True,
                }
            },
        }


class FakeCallResultWriter:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def enqueue_nowait(self, payload: dict) -> bool:
        self.payloads.append(payload)
        return True


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


class FakeRealtimeSession:
    def __init__(
        self,
        model_audio_24k: bytes,
        *,
        reconnect_delay_seconds: float = 0,
        restart_on_interruption: bool = True,
        auto_provider_events: bool = True,
        interruption_error: Exception | None = None,
        interruption_delay_seconds: float = 0,
        connect_error: Exception | None = None,
    ) -> None:
        self.model_audio_24k = model_audio_24k
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.restart_on_interruption = restart_on_interruption
        self.auto_provider_events = auto_provider_events
        self.interruption_error = interruption_error
        self.interruption_delay_seconds = interruption_delay_seconds
        self.connect_error = connect_error
        self.connected = False
        self.closed = False
        self.connect_calls = 0
        self.close_calls = 0
        self.appended_bytes = 0
        self.append_sizes: list[int] = []
        self.cancel_calls = 0
        self.interruption_calls: list[str | None] = []
        self.seed_context_calls: list[str] = []
        self.append_calls = 0
        self.speech_started_turns: list[int] = []
        self.turn_id_starts: list[int] = []
        self.instructions: list[str] = []
        self.speakers: list[str | None] = []
        self.dialog_configs: list[RealtimeDialogConfig | None] = []
        self.tts_texts: list[str] = []
        self.second_turn_announced = False
        self.completed_first_turn = False
        self.on_speech_started: Callable[[int], Awaitable[None]] | None = None
        self.on_input_transcript: Callable[[int, str], Awaitable[None]] | None = None
        self.on_delta: Callable[[int, bytes], Awaitable[None]] | None = None
        self.on_turn_completed: Callable[[RealtimeTurnResult], Awaitable[None]] | None = None

    def bind(
        self,
        on_speech_started: Callable[[int], Awaitable[None]],
        on_input_transcript: Callable[[int, str], Awaitable[None]],
        on_delta: Callable[[int, bytes], Awaitable[None]],
        on_turn_completed: Callable[[RealtimeTurnResult], Awaitable[None]],
        turn_id_start: int,
        instructions: str,
        speaker: str | None = None,
        dialog_config: RealtimeDialogConfig | None = None,
    ):
        self.on_speech_started = on_speech_started
        self.on_input_transcript = on_input_transcript
        self.on_delta = on_delta
        self.on_turn_completed = on_turn_completed
        self.turn_id_starts.append(turn_id_start)
        self.instructions.append(instructions)
        self.speakers.append(speaker)
        self.dialog_configs.append(dialog_config)
        return self

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        if self.connect_calls > 1 and self.reconnect_delay_seconds:
            await asyncio.sleep(self.reconnect_delay_seconds)
        self.connected = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    async def append_audio(self, input_pcm_16k: bytes) -> None:
        self.appended_bytes += len(input_pcm_16k)
        self.append_sizes.append(len(input_pcm_16k))
        self.append_calls += 1
        if not self.auto_provider_events:
            return
        if self.append_calls == 3 and not self.completed_first_turn:
            self.completed_first_turn = True
            asyncio.create_task(self._complete_turn(1))
        if self.append_calls == 4 and not self.second_turn_announced:
            self.second_turn_announced = True
            asyncio.create_task(self._announce_speech_started(2))

    async def cancel_response(self) -> None:
        self.cancel_calls += 1

    async def send_tts_text(self, text: str) -> None:
        self.tts_texts.append(text)

    async def handle_playback_interruption(
        self,
        *,
        interrupted_output_text: str | None = None,
    ) -> None:
        self.interruption_calls.append(interrupted_output_text)
        if self.interruption_delay_seconds:
            await asyncio.sleep(self.interruption_delay_seconds)
        if self.interruption_error is not None:
            raise self.interruption_error
        await self.cancel_response()

    async def seed_assistant_context(
        self,
        text: str,
        *,
        source: str = "external",
    ) -> None:
        del source
        self.seed_context_calls.append(text)

    async def _announce_speech_started(self, turn_id: int) -> None:
        assert self.on_speech_started is not None
        self.speech_started_turns.append(turn_id)
        await self.on_speech_started(turn_id)

    async def _complete_turn(self, turn_id: int) -> None:
        assert self.on_delta is not None
        assert self.on_turn_completed is not None
        await self._announce_speech_started(turn_id)
        await self.on_delta(turn_id, self.model_audio_24k)
        await self.on_turn_completed(
            RealtimeTurnResult(
                turn_id=turn_id,
                input_audio_bytes=self.appended_bytes,
                output_audio_bytes=len(self.model_audio_24k),
                input_transcript="hello",
                output_transcript="hello from model",
                event_counts={
                    "input_audio_buffer.committed": 1,
                    "response.audio.delta": 1,
                    "response.done": 1,
                },
                first_audio_delta_ms=10,
                response_done_ms=20,
            )
        )


def _test_config(
    *,
    tail_silence_ms: int,
    send_interval_ms: int = 10,
    barge_in_enabled: bool = True,
    inbound_rms_diagnostics_enabled: bool = False,
    call_recording: CallRecordingConfig = CallRecordingConfig(),
    handoff_wait_timeout_seconds: int = 180,
) -> GatewayConfig:
    return GatewayConfig(
        freeswitch=FreeSwitchConfig(media_host="127.0.0.1", media_port=0),
        playback=PlaybackConfig(
            send_interval_ms=send_interval_ms,
            tail_silence_ms=tail_silence_ms,
        ),
        vad=VadConfig(
            speech_rms_threshold=300,
            start_speech_ms=20,
            end_silence_ms=40,
            min_speech_ms=20,
            max_utterance_ms=1000,
            pre_speech_ms=0,
            keep_silence_ms=0,
            barge_in_enabled=barge_in_enabled,
        ),
        features=FeatureConfig(
            inbound_rms_diagnostics_enabled=inbound_rms_diagnostics_enabled,
        ),
        call_recording=call_recording,
        handoff=HandoffConfig(wait_timeout_seconds=handoff_wait_timeout_seconds),
    )


def _phone_frame(value: int) -> bytes:
    return samples_to_pcm_s16le([value] * 160)


def _speech_frame(value: int) -> bytes:
    return samples_to_pcm_s16le([value, -value] * 80)
