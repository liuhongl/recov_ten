from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from app.call_control import (
    CallControlError,
    FreeSwitchOutboundDialer,
    HANDOFF_AGENT_BUSY_PROMPT_TEXT,
    OutboundCallManager,
    OutboundCallRecord,
    build_handoff_audio_stream_stop_command,
    build_uuid_bridge_command,
    build_webrtc_agent_originate_command,
    build_originate_command,
    originate_webrtc_agent_test_call,
    parse_create_call_request,
    parse_handoff_claim_request,
    parse_handoff_request,
)
from app.config import (
    CallRecordingConfig,
    EventSocketConfig,
    FeatureConfig,
    FlowCallbackConfig,
    GatewayConfig,
    OutboundCallConfig,
)
from app.freeswitch_event_socket import ChannelStateEvent
from app.opening import (
    OpeningAudio,
    OpeningAudioStore,
    OpeningGenerationFailed,
    OpeningGenerationTimeout,
    parse_opening_request,
)
from app.audio_codec import samples_to_pcm_s16le
from app.flow_callback import FlowCallbackEvent
from app.postgres import (
    BusinessPromptPreparation,
    PostgresCallResultWriter,
    PromptSnapshot,
)


def test_build_originate_command_uses_local_dialplan():
    record = OutboundCallRecord(
        call_id="call-1",
        external_call_id="biz-1",
        destination="1000",
        endpoint="user/1000",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="AI",
        caller_id_number="9000",
        originate_timeout_seconds=20,
        context={"customer_id": "c-1"},
    )

    command = build_originate_command(record)

    assert command.startswith("originate {")
    assert "origination_uuid=call-1" in command
    assert "origination_caller_id_number=9000" in command
    assert "ignore_early_media=true" in command
    assert "sip_realtime_external_call_id=biz-1" in command
    assert command.endswith("}user/1000 9199 XML default")


def test_freeswitch_outbound_dialer_starts_read_only_recording_with_channel_vars():
    commands: list[str] = []

    class FakeClient:
        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def api(self, command: str) -> str:
            commands.append(command)
            return "+OK Success"

    dialer = FreeSwitchOutboundDialer(GatewayConfig())
    dialer._make_client = lambda: FakeClient()  # type: ignore[method-assign]

    reply = asyncio.run(
        dialer.start_recording(
            "channel-uuid-1",
            "/tmp/handoff-customer.wav",
            read_only=True,
        )
    )

    assert reply == "+OK Success"
    assert commands == [
        "uuid_setvar channel-uuid-1 RECORD_READ_ONLY true",
        "uuid_setvar channel-uuid-1 RECORD_WRITE_ONLY false",
        "uuid_record channel-uuid-1 start /tmp/handoff-customer.wav",
    ]


def test_outbound_manager_adds_full_call_recording_path_from_business_call_id():
    commands: list[str] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            commands.append(command)
            return "+OK call-1"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            call_recording=CallRecordingConfig(
                enabled=True,
                directory="/var/lib/freeswitch/recordings",
            ),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )

        final_call = _wait_for_status(manager, call["call_id"], "originated")

        assert final_call["recording_path"] == (
            "/var/lib/freeswitch/recordings/990000000000032001.wav"
        )
        assert commands
        assert (
            "sip_realtime_recording_path="
            "/var/lib/freeswitch/recordings/990000000000032001.wav"
        ) in commands[0]
        assert f"origination_uuid={call['call_id']}" in commands[0]
    finally:
        manager.shutdown()


def test_build_webrtc_agent_originate_command_parks_known_agent_uuid():
    command = build_webrtc_agent_originate_command(
        agent_uuid="agent-uuid-1",
        endpoint="sofia/internal/sip:1001@127.0.0.1:5066;transport=ws",
        timeout_seconds=12,
    )

    assert command.startswith("originate {")
    assert "origination_uuid=agent-uuid-1" in command
    assert "originate_timeout=12" in command
    assert "origination_caller_id_number=9001" in command
    assert command.endswith(
        "}sofia/internal/sip:1001@127.0.0.1:5066;transport=ws &park()"
    )


def test_build_handoff_audio_stream_stop_command_stops_media_bug():
    command = build_handoff_audio_stream_stop_command(call_id="customer-call-1")

    assert command == "uuid_audio_stream customer-call-1 stop"


def test_build_uuid_bridge_command_uses_customer_and_agent_uuids():
    command = build_uuid_bridge_command(
        customer_call_id="customer-uuid-1",
        agent_uuid="agent-uuid-1",
    )

    assert command == "uuid_bridge customer-uuid-1 agent-uuid-1"


def test_parse_handoff_request_rejects_zero_wait_timeout():
    with pytest.raises(CallControlError) as exc_info:
        parse_handoff_request({"wait_timeout_seconds": 0})

    assert "wait_timeout_seconds must be between 1 and 300" in str(exc_info.value)


def test_parse_handoff_request_defaults_to_sixty_second_wait_timeout():
    request = parse_handoff_request({"last_utterance": "我要转人工"})

    assert request.wait_timeout_seconds == 60


def test_parse_handoff_claim_request_rejects_zero_timeout():
    with pytest.raises(CallControlError) as exc_info:
        parse_handoff_claim_request({"timeout_seconds": 0})

    assert "timeout_seconds must be between 1 and 120" in str(exc_info.value)


def test_originate_webrtc_agent_test_call_maps_event_socket_failure(monkeypatch):
    class FakeDialer:
        def __init__(self, config):
            self.config = config

        async def resolve_endpoint(self, endpoint: str) -> str:
            raise OSError("connect failed")

    monkeypatch.setattr("app.call_control.FreeSwitchOutboundDialer", FakeDialer)
    config = GatewayConfig(event_socket=EventSocketConfig(enabled=True))

    with pytest.raises(CallControlError) as exc_info:
        originate_webrtc_agent_test_call(config, {"agent_extension": "1001"})

    assert exc_info.value.status_code == 503
    assert "FreeSWITCH Event Socket request failed" in str(exc_info.value)


def test_outbound_manager_records_agent_takeover_suggestion_without_handoff():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call-1"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        active_call = _wait_for_status(manager, call["call_id"], "originated")

        suggested_call = manager.record_agent_takeover_suggestion(
            active_call["call_id"],
            {
                "reason": "complaint",
                "last_utterance": "我想投诉",
            },
        )

        assert suggested_call["status"] == "originated"
        assert suggested_call["handoff"] is None
        assert suggested_call["agent_takeover_suggestion"] == {
            "state": "suggested",
            "reason": "complaint",
            "last_utterance": "我想投诉",
            "suggested_at_ms": suggested_call["agent_takeover_suggestion"][
                "suggested_at_ms"
            ],
            "updated_at_ms": suggested_call["agent_takeover_suggestion"][
                "updated_at_ms"
            ],
            "can_takeover": True,
        }
    finally:
        manager.shutdown()


def test_outbound_manager_disables_takeover_suggestion_after_terminal_status():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call-1"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}")
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        active_call = _wait_for_status(manager, call["call_id"], "originated")
        manager.record_agent_takeover_suggestion(
            active_call["call_id"],
            {"reason": "complaint", "last_utterance": "我想投诉"},
        )

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=active_call["call_id"],
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = manager.get_call(active_call["call_id"])
        assert final_call is not None
        assert final_call["agent_takeover_suggestion"]["can_takeover"] is False
    finally:
        manager.shutdown()


def test_outbound_manager_handoff_creates_waiting_agent_before_claim():
    operations: list[tuple[str, str, str | None]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            operations.append(("resolve", endpoint, None))
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            operations.append(("originate", command, None))
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            operations.append(("break_audio_stream", call_id, None))
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            operations.append(("bridge", customer_call_id, agent_uuid))
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        final_call = _wait_for_status(manager, call["call_id"], "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=final_call["call_id"])
        )
        operations.clear()

        handoff_call = manager.request_handoff(
            final_call["call_id"],
            {
                "trigger": "customer_requested",
                "reason": "request_human",
                "last_utterance": "我要转人工",
                "ai_turns": [
                    {"role": "assistant", "text": "您好，这里是物业中心。"},
                    {"role": "user", "text": "我要转人工"},
                ],
            },
        )

        assert handoff_call["status"] == "waiting_agent"
        assert handoff_call["handoff"] == {
            "state": "waiting_agent",
            "trigger": "customer_requested",
            "reason": "request_human",
            "last_utterance": "我要转人工",
            "summary": None,
            "requested_at_ms": handoff_call["handoff"]["requested_at_ms"],
            "expires_at_ms": handoff_call["handoff"]["expires_at_ms"],
            "claimed_at_ms": None,
            "claimed_by": None,
            "agent_extension": None,
            "agent_uuid": None,
            "agent_endpoint": None,
            "answered_at_ms": None,
            "bridged_at_ms": None,
            "human_ended_at_ms": None,
            "updated_at_ms": handoff_call["handoff"]["updated_at_ms"],
            "agent_originate_reply": None,
            "audio_stream_break_reply": None,
            "bridge_reply": None,
            "human_transcript_status": None,
            "human_transcript_error": None,
            "recording_status": None,
            "recording_error": None,
            "customer_recording_path": None,
            "agent_recording_path": None,
            "customer_recording_host_path": None,
            "agent_recording_host_path": None,
            "recording_started_at_ms": None,
            "recording_stopped_at_ms": None,
            "ai_turns": [
                {"role": "assistant", "text": "您好，这里是物业中心。"},
                {"role": "user", "text": "我要转人工"},
            ],
            "human_turns": [],
            "turns": [
                {"role": "assistant", "text": "您好，这里是物业中心。"},
                {"role": "user", "text": "我要转人工"},
            ],
            "recent_turns": [
                {"role": "assistant", "text": "您好，这里是物业中心。"},
                {"role": "user", "text": "我要转人工"},
            ],
            "can_claim": True,
            "error": None,
        }
        assert handoff_call["turns"] == handoff_call["handoff"]["turns"]
        assert handoff_call["recent_turns"] == handoff_call["handoff"]["recent_turns"]
        assert operations == []

        claimed_call = manager.claim_handoff(
            final_call["call_id"],
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "timeout_seconds": 12,
                "claimed_by": "agent-1001",
            },
        )

        assert claimed_call["status"] == "human_active"
        assert claimed_call["handoff"]["state"] == "human_active"
        assert claimed_call["handoff"]["claimed_by"] == "agent-1001"
        assert claimed_call["handoff"]["agent_extension"] == "1001"
        assert claimed_call["handoff"]["agent_uuid"] == "agent-uuid-1"
        assert claimed_call["handoff"]["agent_endpoint"] == (
            "sofia/internal/sip:agent@browser.invalid;transport=ws"
        )
        assert claimed_call["handoff"]["agent_originate_reply"] == "+OK agent-uuid-1"
        assert claimed_call["handoff"]["audio_stream_break_reply"] == "+OK"
        assert claimed_call["handoff"]["bridge_reply"] == "+OK uuid_bridge accepted"
        assert claimed_call["handoff"]["human_transcript_status"] == "pending"
        assert claimed_call["handoff"]["recording_status"] == "disabled"
        assert claimed_call["handoff"]["can_claim"] is False
        assert operations == [
            ("resolve", "sofia_contact:*/1001", None),
            (
                "originate",
                (
                    "originate {origination_uuid=agent-uuid-1,"
                    "origination_caller_id_name=Handoff_Test,"
                    "origination_caller_id_number=9001,originate_timeout=12}"
                    "sofia/internal/sip:agent@browser.invalid;transport=ws &park()"
                ),
                None,
            ),
            ("break_audio_stream", final_call["call_id"], None),
            ("bridge", final_call["call_id"], "agent-uuid-1"),
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_expires_waiting_handoff_plays_busy_notice_before_hangup(
    tmp_path,
):
    operations: list[tuple[str, str, str | None]] = []
    generated_texts: list[str] = []

    class FakeOpeningGenerator:
        def generate(self, opening):
            generated_texts.append(opening.opening_text)
            return OpeningAudio(
                pcm16=samples_to_pcm_s16le([1200] * 480),
                sample_rate=24000,
                generation_ms=100,
            )

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def play_file(self, call_id: str, path: str) -> str:
            operations.append(("play_file", call_id, path))
            return "+OK playback accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            call_recording=CallRecordingConfig(
                enabled=True,
                directory="/var/lib/freeswitch/recordings",
                host_directory=str(tmp_path),
            ),
        ),
        dialer_factory=lambda: FakeDialer(),
        opening_generator=FakeOpeningGenerator(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {"last_utterance": "我要转人工", "wait_timeout_seconds": 1},
        )

        expired_call = _wait_for_status(manager, call_id, "hangup_sent")

        assert expired_call["handoff"]["state"] == "handoff_failed"
        assert expired_call["handoff"]["error"] == "handoff request expired"
        assert expired_call["handoff"]["can_claim"] is False
        assert generated_texts == [HANDOFF_AGENT_BUSY_PROMPT_TEXT]
        assert operations[0][0:2] == ("play_file", call_id)
        assert operations[0][2].startswith(
            "/var/lib/freeswitch/recordings/handoff-prompts/"
        )
        assert operations[1:] == [("hangup", call_id, "NORMAL_CLEARING")]
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_callback_after_handoff_expires_and_call_ends():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {"last_utterance": "我要转人工", "wait_timeout_seconds": 1},
        )
        with manager._lock:
            record = manager._calls[call_id]
            assert record.handoff is not None
            expired_at_ms = record.handoff.requested_at_ms - 1
            record.handoff.expires_at_ms = expired_at_ms

        manager._expire_handoff_request(call_id, expired_at_ms)
        _wait_for_status(manager, call_id, "hangup_sent")
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-1"
        assert flow_events[-1].business_id == "990000000000032001"
        assert flow_events[-1].message == "转人工失败"
    finally:
        manager.shutdown()


def test_outbound_manager_does_not_emit_duplicate_callback_for_handoff_terminal_failure():
    flow_events: list[FlowCallbackEvent] = []
    call_record_events: list[tuple[str, dict]] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            call_record_events.append(("started", context))
            return True

        def mark_failed(self, context):
            call_record_events.append(("failed", context))
            return True

        def mark_no_answer(self, context):
            call_record_events.append(("no_answer", context))
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="ORIGINATOR_CANCEL",
            )
        )

        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].message == "转人工失败"
        assert call_record_events == [
            (
                "started",
                {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            ),
            (
                "failed",
                {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            ),
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_hangs_up_customer_when_claim_finds_expired_handoff():
    operations: list[tuple[str, str, str | None]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        handoff_call = manager.request_handoff(
            call_id,
            {"last_utterance": "我要转人工", "wait_timeout_seconds": 1},
        )
        with manager._lock:
            record = manager._calls[call_id]
            assert record.handoff is not None
            record.handoff.expires_at_ms = handoff_call["handoff"]["requested_at_ms"] - 1

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )
        expired_call = _wait_for_status(manager, call_id, "hangup_sent")

        assert exc_info.value.status_code == 409
        assert "handoff request expired" in str(exc_info.value)
        assert expired_call["handoff"]["state"] == "handoff_failed"
        assert expired_call["handoff"]["error"] == "handoff request expired"
        assert expired_call["handoff"]["can_claim"] is False
        assert operations == [("hangup", call_id, "NORMAL_CLEARING")]
    finally:
        manager.shutdown()


def test_outbound_manager_rejects_handoff_after_hangup_is_sent():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "user/1000"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_hangup(call_id)
        _wait_for_status(manager, call_id, "hangup_sent")

        with pytest.raises(CallControlError) as exc_info:
            manager.request_handoff(call_id, {"last_utterance": "我要转人工"})

        final_call = manager.get_call(call_id)
        assert final_call is not None
        assert exc_info.value.status_code == 409
        assert "call is not active" in str(exc_info.value)
        assert final_call["status"] == "hangup_sent"
        assert final_call["handoff"] is None
    finally:
        manager.shutdown()


def test_outbound_manager_rejects_handoff_claim_after_hangup_is_sent():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.request_hangup(call_id)
        _wait_for_status(manager, call_id, "hangup_sent")

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )

        final_call = manager.get_call(call_id)
        assert final_call is not None
        assert exc_info.value.status_code == 409
        assert "call is not active" in str(exc_info.value)
        assert final_call["status"] == "hangup_sent"
        assert final_call["handoff"]["state"] == "handoff_failed"
        assert final_call["handoff"]["error"] == (
            "customer hangup requested before handoff connected"
        )
        assert final_call["handoff"]["can_claim"] is False
    finally:
        manager.shutdown()


def test_outbound_manager_marks_waiting_handoff_failed_when_customer_hangs_up():
    operations: list[tuple[str, str, str | None]] = []
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {"last_utterance": "我要转人工", "wait_timeout_seconds": 30},
        )

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = manager.get_call(call_id)
        assert final_call is not None
        assert final_call["status"] == "completed"
        assert final_call["handoff"]["state"] == "handoff_failed"
        assert final_call["handoff"]["error"] == (
            "customer hung up before handoff connected"
        )
        assert final_call["handoff"]["can_claim"] is False
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-1"
        assert flow_events[-1].business_id == "990000000000032001"
        assert flow_events[-1].message == "转人工失败"
        assert operations == []
    finally:
        manager.shutdown()


def test_outbound_manager_handoff_transcript_merges_ai_and_human_turns_after_hangup():
    enqueued_payloads = []

    class FakeCallResultWriter:
        def enqueue_nowait(self, payload):
            enqueued_payloads.append(payload)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            call_recording=CallRecordingConfig(
                enabled=True,
                directory="/var/lib/freeswitch/recordings",
            ),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_result_writer=FakeCallResultWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {
                "trigger": "customer_requested",
                "reason": "request_human",
                "last_utterance": "我要转人工",
                "ai_turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                ],
            },
        )
        manager.claim_handoff(
            call_id,
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "claimed_by": "agent-1001",
            },
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = manager.complete_handoff_transcript(
            call_id,
            {
                "turns": [
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "agent-1001",
                        "text": "您好，我是物业客服。",
                        "start_ms": 1200,
                        "end_ms": 2600,
                        "confidence": 0.92,
                    },
                    {
                        "role": "user",
                        "speaker_type": "customer",
                        "text": "我想确认一下费用。",
                    },
                ]
            },
        )

        assert final_call["handoff"]["human_transcript_status"] == "completed"
        assert final_call["handoff"]["human_ended_at_ms"] is not None
        assert enqueued_payloads == [
            {
                "call_id": call_id,
                "business_id": "990000000000032001",
                "recording_path": (
                    "/var/lib/freeswitch/recordings/990000000000032001.wav"
                ),
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
                "turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "agent-1001",
                        "text": "您好，我是物业客服。",
                        "start_ms": 1200,
                        "end_ms": 2600,
                        "confidence": 0.92,
                    },
                    {
                        "role": "user",
                        "speaker_type": "customer",
                        "text": "我想确认一下费用。",
                    },
                ],
            }
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_retries_handoff_transcript_after_result_queue_full():
    enqueued_payloads = []

    class FakeCallResultWriter:
        def __init__(self):
            self.calls = 0

        def enqueue_nowait(self, payload):
            self.calls += 1
            if self.calls == 1:
                return False
            enqueued_payloads.append(payload)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    writer = FakeCallResultWriter()
    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_result_writer=writer,
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {
                "last_utterance": "我要转人工",
                "ai_turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                ],
            },
        )
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        transcript_payload = {
            "turns": [
                {
                    "role": "assistant",
                    "speaker_type": "human_agent",
                    "agent_id": "agent-1001",
                    "text": "您好，我是人工客服。",
                }
            ]
        }
        with pytest.raises(CallControlError) as exc_info:
            manager.complete_handoff_transcript(call_id, transcript_payload)

        retryable_call = manager.get_call(call_id)
        retried_call = manager.complete_handoff_transcript(call_id, transcript_payload)

        assert exc_info.value.status_code == 503
        assert retryable_call["handoff"]["human_transcript_status"] == "pending"
        assert retryable_call["handoff"]["turns"] == [
            {"role": "assistant", "speaker_type": "ai", "text": "您好"},
            {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
        ]
        assert retried_call["handoff"]["human_transcript_status"] == "completed"
        assert writer.calls == 2
        assert len(enqueued_payloads) == 1
        assert enqueued_payloads[0]["turns"][-1]["text"] == "您好，我是人工客服。"
    finally:
        manager.shutdown()


def test_outbound_manager_ignores_late_transcript_updates_after_completed():
    enqueued_payloads = []

    class FakeCallResultWriter:
        def enqueue_nowait(self, payload):
            enqueued_payloads.append(payload)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_result_writer=FakeCallResultWriter(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {
                "last_utterance": "我要转人工",
                "ai_turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                ],
            },
        )
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        first_call = manager.complete_handoff_transcript(
            call_id,
            {
                "turns": [
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "1001",
                        "text": "第一次人工转写。",
                    }
                ]
            },
        )
        duplicate_call = manager.complete_handoff_transcript(
            call_id,
            {
                "turns": [
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "1001",
                        "text": "迟到的重复转写。",
                    }
                ]
            },
        )
        late_failed_call = manager.complete_handoff_transcript(
            call_id,
            {"status": "failed", "error": "late asr failure"},
        )

        assert len(enqueued_payloads) == 1
        assert first_call["handoff"]["turns"] == duplicate_call["handoff"]["turns"]
        assert late_failed_call["handoff"]["human_transcript_status"] == "completed"
        assert late_failed_call["handoff"]["human_transcript_error"] is None
        assert late_failed_call["handoff"]["turns"][-1]["text"] == "第一次人工转写。"
    finally:
        manager.shutdown()


def test_outbound_manager_defaults_error_and_ignores_late_success_after_failed_callback():
    enqueued_payloads = []
    flow_events: list[FlowCallbackEvent] = []

    class FakeCallResultWriter:
        def enqueue_nowait(self, payload):
            enqueued_payloads.append(payload)
            return True

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_result_writer=FakeCallResultWriter(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        failed_call = manager.complete_handoff_transcript(call_id, {"status": "failed"})
        late_success_call = manager.complete_handoff_transcript(
            call_id,
            {
                "turns": [
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "text": "迟到的成功转写。",
                    }
                ]
            },
        )

        assert failed_call["handoff"]["human_transcript_status"] == "failed"
        assert failed_call["handoff"]["human_transcript_error"] == (
            "human transcript failed"
        )
        assert late_success_call["handoff"]["human_transcript_status"] == "failed"
        assert late_success_call["handoff"]["human_transcript_error"] == (
            "human transcript failed"
        )
        assert enqueued_payloads == []
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
    finally:
        manager.shutdown()


def test_outbound_manager_rejects_handoff_transcript_before_human_hangup():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )

        with pytest.raises(CallControlError) as exc_info:
            manager.complete_handoff_transcript(
                call_id,
                {"turns": [{"role": "assistant", "text": "您好，我是物业客服。"}]},
            )

        assert exc_info.value.status_code == 409
        assert "human handoff is still active" in str(exc_info.value)
    finally:
        manager.shutdown()


def test_outbound_manager_hangs_up_agent_channel_after_human_handoff_ends():
    operations: list[tuple[str, str, str | None]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if ("hangup", "agent-uuid-1", "NORMAL_CLEARING") in operations:
                break
            time.sleep(0.02)

        final_call = manager.get_call(call_id)
        assert final_call is not None
        assert final_call["status"] == "completed"
        assert final_call["handoff"]["state"] == "completed"
        assert ("hangup", "agent-uuid-1", "NORMAL_CLEARING") in operations
    finally:
        manager.shutdown()


def test_outbound_manager_handoff_records_temp_audio_until_hangup_when_enabled():
    operations: list[tuple[str, str, str, bool] | tuple[str, str, str]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            operations.append(("record_start", channel_uuid, path, read_only))
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            operations.append(("record_stop", channel_uuid, path))
            return "+OK Success"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(
                recording_enabled=True,
                recording_dir="/tmp/recov_ten_handoff_test",
                recording_host_dir="./freeswitch-local/recordings/handoff",
            ),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})

        claimed_call = manager.claim_handoff(
            call_id,
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "claimed_by": "agent-1001",
            },
        )

        handoff = claimed_call["handoff"]
        assert handoff["state"] == "human_active"
        assert handoff["human_transcript_status"] == "pending"
        assert handoff["recording_status"] == "recording"
        assert handoff["customer_recording_path"] == (
            f"/tmp/recov_ten_handoff_test/{call_id}-customer.wav"
        )
        assert handoff["agent_recording_path"] == (
            f"/tmp/recov_ten_handoff_test/{call_id}-agent-uuid-1-agent.wav"
        )
        assert handoff["customer_recording_host_path"] == (
            f"./freeswitch-local/recordings/handoff/{call_id}-customer.wav"
        )
        assert handoff["agent_recording_host_path"] == (
            "./freeswitch-local/recordings/handoff/"
            f"{call_id}-agent-uuid-1-agent.wav"
        )
        assert operations == [
            (
                "record_start",
                call_id,
                f"/tmp/recov_ten_handoff_test/{call_id}-customer.wav",
                True,
            ),
            (
                "record_start",
                "agent-uuid-1",
                f"/tmp/recov_ten_handoff_test/{call_id}-agent-uuid-1-agent.wav",
                True,
            ),
        ]

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = _wait_for_handoff_recording_status(manager, call_id, "completed")
        assert final_call["handoff"]["state"] == "completed"
        assert final_call["handoff"]["recording_stopped_at_ms"] is not None
        assert operations[-2:] == [
            (
                "record_stop",
                call_id,
                f"/tmp/recov_ten_handoff_test/{call_id}-customer.wav",
            ),
            (
                "record_stop",
                "agent-uuid-1",
                f"/tmp/recov_ten_handoff_test/{call_id}-agent-uuid-1-agent.wav",
            ),
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_treats_missing_session_on_recording_stop_as_completed():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "-ERR Cannot locate session!"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(recording_enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = _wait_for_handoff_recording_status(manager, call_id, "completed")
        assert final_call["handoff"]["recording_error"] is None
    finally:
        manager.shutdown()


def test_outbound_manager_marks_transcript_failed_when_recording_stop_fails():
    class FakeProcessor:
        def process(self, job):
            raise AssertionError("transcript processor should not run")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "-ERR disk full"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(recording_enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
        handoff_transcript_processor=FakeProcessor(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = _wait_for_handoff_transcript_status(manager, call_id, "failed")
        assert final_call["handoff"]["recording_status"] == "failed"
        assert final_call["handoff"]["human_transcript_error"] == (
            "recording failed: -ERR disk full; -ERR disk full"
        )
    finally:
        manager.shutdown()


def test_outbound_manager_processes_handoff_recordings_after_recording_completed(
    tmp_path,
):
    enqueued_payloads = []
    processor_jobs = []

    class FakeCallResultWriter:
        def enqueue_nowait(self, payload):
            enqueued_payloads.append(payload)
            return True

    class FakeProcessor:
        def process(self, job):
            processor_jobs.append(job)
            return [
                {
                    "role": "assistant",
                    "speaker_type": "human_agent",
                    "agent_id": job["agent_id"],
                    "text": "您好，我是物业客服。",
                },
                {
                    "role": "user",
                    "speaker_type": "customer",
                    "text": "我想确认一下物业费。",
                },
            ]

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "-ERR Cannot locate session!"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(
                recording_enabled=True,
                recording_dir="/container/tmp/recov_ten_handoff",
                recording_host_dir=str(tmp_path),
            ),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_result_writer=FakeCallResultWriter(),
        handoff_transcript_processor=FakeProcessor(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {
                "last_utterance": "我要转人工",
                "ai_turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                ],
            },
        )
        manager.claim_handoff(
            call_id,
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "claimed_by": "agent-1001",
            },
        )
        customer_recording_path = tmp_path / f"{call_id}-customer.wav"
        agent_recording_path = tmp_path / f"{call_id}-agent-uuid-1-agent.wav"
        customer_recording_path.write_bytes(b"customer wav")
        agent_recording_path.write_bytes(b"agent wav")
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = _wait_for_handoff_transcript_status(manager, call_id, "completed")

        assert processor_jobs == [
            {
                "call_id": call_id,
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
                "agent_id": "agent-1001",
                "agent_uuid": "agent-uuid-1",
                "customer_recording_path": str(customer_recording_path),
                "agent_recording_path": str(agent_recording_path),
            }
        ]
        assert not customer_recording_path.exists()
        assert not agent_recording_path.exists()
        assert final_call["handoff"]["human_transcript_status"] == "completed"
        assert enqueued_payloads == [
            {
                "call_id": call_id,
                "business_id": "990000000000032001",
                "recording_path": None,
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
                "turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "agent-1001",
                        "text": "您好，我是物业客服。",
                    },
                    {
                        "role": "user",
                        "speaker_type": "customer",
                        "text": "我想确认一下物业费。",
                    },
                ],
            }
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_marks_handoff_transcript_failed_when_processor_fails():
    class BrokenProcessor:
        def process(self, job):
            raise RuntimeError("asr unavailable")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "+OK Success"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(recording_enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
        handoff_transcript_processor=BrokenProcessor(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = _wait_for_handoff_transcript_status(manager, call_id, "failed")
        assert final_call["handoff"]["human_transcript_error"] == "asr unavailable"
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_flow_callback_when_handoff_asr_fails():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class BrokenProcessor:
        def process(self, job):
            raise RuntimeError("asr unavailable")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "+OK Success"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(recording_enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
        handoff_transcript_processor=BrokenProcessor(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        _wait_for_handoff_transcript_status(manager, call_id, "failed")
        _wait_for_flow_event_count(flow_events, 2)
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-1"
        assert flow_events[-1].business_id == "990000000000032001"
        assert flow_events[-1].message == "人工转写失败"
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_flow_callback_when_handoff_recording_fails():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeProcessor:
        def process(self, job):
            raise AssertionError("processor should not run when recording fails")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "-ERR disk full"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(recording_enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
        handoff_transcript_processor=FakeProcessor(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        _wait_for_handoff_transcript_status(manager, call_id, "failed")
        _wait_for_flow_event_count(flow_events, 2)
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-1"
        assert flow_events[-1].business_id == "990000000000032001"
        assert flow_events[-1].message == "人工转写失败"
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_callback_when_handoff_recording_paths_are_missing():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeProcessor:
        def process(self, job):
            raise AssertionError("processor should not run without recording paths")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
        handoff_transcript_processor=FakeProcessor(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})
        manager.claim_handoff(
            call_id,
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "claimed_by": "agent-1001",
            },
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )
        with manager._lock:
            record = manager._calls[call_id]
            assert record.handoff is not None
            record.handoff.recording_status = "completed"
            record.handoff.customer_recording_path = None
            record.handoff.agent_recording_path = None

        manager._maybe_submit_handoff_transcript_processor(call_id)

        final_call = _wait_for_handoff_transcript_status(manager, call_id, "failed")
        assert final_call["handoff"]["human_transcript_error"] == (
            "recording path is missing"
        )
        _wait_for_flow_event_count(flow_events, 2)
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-1"
        assert flow_events[-1].business_id == "990000000000032001"
        assert flow_events[-1].message == "人工转写失败"
    finally:
        manager.shutdown()


def test_outbound_manager_auto_transcript_writes_record_before_success_callback():
    writer_events = []

    class Store:
        async def mark_transcript_completed(self, context, transcript_json):
            writer_events.append(
                ("store", dict(context), json.loads(transcript_json))
            )
            return True

    class FakeFlowCallbackWriter:
        def publish(self, event: FlowCallbackEvent):
            writer_events.append(("callback", event))
            return True

    writer = PostgresCallResultWriter(
        Store(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )
    loop = asyncio.new_event_loop()
    writer_ready = threading.Event()

    def run_writer_loop():
        asyncio.set_event_loop(loop)

        async def start_writer():
            writer.start()
            writer_ready.set()

        loop.run_until_complete(start_writer())
        loop.run_forever()
        loop.run_until_complete(writer.stop())
        loop.close()

    writer_thread = threading.Thread(target=run_writer_loop)
    writer_thread.start()
    assert writer_ready.wait(timeout=1.0)

    class FakeProcessor:
        def process(self, job):
            return [
                {
                    "role": "assistant",
                    "speaker_type": "human_agent",
                    "agent_id": job["agent_id"],
                    "text": "您好，我是物业客服。",
                },
                {
                    "role": "user",
                    "speaker_type": "customer",
                    "text": "我想确认一下费用。",
                },
            ]

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "+OK uuid_bridge accepted"

        async def start_recording(
            self,
            channel_uuid: str,
            path: str,
            *,
            read_only: bool = False,
        ) -> str:
            return "+OK Success"

        async def stop_recording(self, channel_uuid: str, path: str) -> str:
            return "+OK Success"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
            features=FeatureConfig(recording_enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
        call_result_writer=writer,
        handoff_transcript_processor=FakeProcessor(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {
                "last_utterance": "我要转人工",
                "ai_turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                ],
            },
        )
        manager.claim_handoff(
            call_id,
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "claimed_by": "agent-1001",
            },
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = _wait_for_handoff_transcript_status(manager, call_id, "completed")
        _wait_for_writer_event_count(writer_events, 2)

        assert final_call["handoff"]["human_transcript_status"] == "completed"
        assert writer_events[0] == (
            "store",
            {
                "tenantId": "000000",
                "taskId": "task-1",
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            },
            {
                "turns": [
                    {"role": "assistant", "speaker_type": "ai", "text": "您好"},
                    {"role": "user", "speaker_type": "customer", "text": "我要转人工"},
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "agent-1001",
                        "text": "您好，我是物业客服。",
                    },
                    {
                        "role": "user",
                        "speaker_type": "customer",
                        "text": "我想确认一下费用。",
                    },
                ]
            },
        )
        assert writer_events[1][0] == "callback"
        callback_event = writer_events[1][1]
        assert callback_event.status == "SUCCESS"
        assert callback_event.tenant_id == "000000"
        assert callback_event.task_id == "task-1"
        assert callback_event.business_id == "990000000000032001"
        assert callback_event.message == "外呼完成，转写已写入"
    finally:
        manager.shutdown()
        loop.call_soon_threadsafe(loop.stop)
        writer_thread.join(timeout=3.0)


def test_outbound_manager_releases_handoff_claim_when_agent_originate_fails():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            if "&park()" not in command:
                return "+OK customer-call"
            return "-ERR USER_NOT_REGISTERED"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )

        assert exc_info.value.status_code == 503
        failed_call = manager.get_call(call_id)
        assert failed_call is not None
        assert failed_call["status"] == "waiting_agent"
        assert failed_call["handoff"]["state"] == "waiting_agent"
        assert failed_call["handoff"]["error"] == "-ERR USER_NOT_REGISTERED"
        assert failed_call["handoff"]["agent_uuid"] is None
        assert failed_call["handoff"]["claimed_at_ms"] is None
        assert failed_call["handoff"]["can_claim"] is True
    finally:
        manager.shutdown()


def test_outbound_manager_releases_handoff_claim_and_hangs_up_agent_when_bridge_fails():
    operations: list[tuple[str, str, str]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            return "-ERR No such channel"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )

        assert exc_info.value.status_code == 503
        failed_call = manager.get_call(call_id)
        assert failed_call is not None
        assert failed_call["status"] == "waiting_agent"
        assert failed_call["handoff"]["state"] == "waiting_agent"
        assert failed_call["handoff"]["agent_uuid"] is None
        assert failed_call["handoff"]["audio_stream_break_reply"] is None
        assert failed_call["handoff"]["error"] == "-ERR No such channel"
        assert failed_call["handoff"]["can_claim"] is True
        assert operations == [("hangup", "agent-uuid-1", "NORMAL_CLEARING")]
    finally:
        manager.shutdown()


def test_outbound_manager_hangs_up_agent_when_break_audio_stream_fails():
    operations: list[tuple[str, str, str | None]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            return "+OK agent-uuid-1"

        async def break_audio_stream(self, call_id: str) -> str:
            operations.append(("break_audio_stream", call_id, None))
            raise CallControlError("-ERR audio stream not found", status_code=503)

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(call_id, {"last_utterance": "我要转人工"})

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )

        failed_call = manager.get_call(call_id)
        assert failed_call is not None
        assert exc_info.value.status_code == 503
        assert failed_call["status"] == "waiting_agent"
        assert failed_call["handoff"]["state"] == "waiting_agent"
        assert failed_call["handoff"]["agent_uuid"] is None
        assert failed_call["handoff"]["error"] == "-ERR audio stream not found"
        assert operations == [
            ("break_audio_stream", call_id, None),
            ("hangup", "agent-uuid-1", "NORMAL_CLEARING"),
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_stops_handoff_when_customer_hangs_up_after_agent_originate():
    operations: list[tuple[str, str, str | None]] = []
    manager: OutboundCallManager
    customer_call_id = ""

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            if "&park()" in command:
                manager.handle_channel_event(
                    ChannelStateEvent(
                        name="CHANNEL_HANGUP_COMPLETE",
                        call_id=customer_call_id,
                        hangup_cause="NORMAL_CLEARING",
                    )
                )
                return "+OK agent-uuid-1"
            return "+OK customer-call"

        async def break_audio_stream(self, call_id: str) -> str:
            operations.append(("break_audio_stream", call_id, None))
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            operations.append(("bridge", customer_call_id, agent_uuid))
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        customer_call_id = call["call_id"]
        _wait_for_status(manager, customer_call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=customer_call_id)
        )
        manager.request_handoff(customer_call_id, {"last_utterance": "我要转人工"})

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                customer_call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )

        final_call = manager.get_call(customer_call_id)
        assert final_call is not None
        assert exc_info.value.status_code == 409
        assert "customer call ended before handoff connected" in str(exc_info.value)
        assert final_call["status"] == "completed"
        assert final_call["handoff"]["state"] == "handoff_failed"
        assert final_call["handoff"]["error"] == (
            "customer call ended before handoff connected"
        )
        assert final_call["handoff"]["can_claim"] is False
        assert operations == [("hangup", "agent-uuid-1", "NORMAL_CLEARING")]
    finally:
        manager.shutdown()


def test_outbound_manager_does_not_reopen_handoff_when_hangup_sent_during_claim():
    operations: list[tuple[str, str, str | None]] = []
    manager: OutboundCallManager
    customer_call_id = ""

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            if "&park()" in command:
                manager.request_hangup(customer_call_id)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    call = manager.get_call(customer_call_id)
                    if call and call["status"] == "hangup_sent":
                        break
                    time.sleep(0.02)
                return "+OK agent-uuid-1"
            return "+OK customer-call"

        async def break_audio_stream(self, call_id: str) -> str:
            operations.append(("break_audio_stream", call_id, None))
            return "+OK"

        async def bridge(self, customer_call_id: str, agent_uuid: str) -> str:
            operations.append(("bridge", customer_call_id, agent_uuid))
            return "+OK uuid_bridge accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        customer_call_id = call["call_id"]
        _wait_for_status(manager, customer_call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=customer_call_id)
        )
        manager.request_handoff(customer_call_id, {"last_utterance": "我要转人工"})

        with pytest.raises(CallControlError) as exc_info:
            manager.claim_handoff(
                customer_call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )

        final_call = manager.get_call(customer_call_id)
        assert final_call is not None
        assert exc_info.value.status_code == 409
        assert "customer call ended before handoff connected" in str(exc_info.value)
        assert final_call["status"] == "hangup_sent"
        assert final_call["handoff"]["state"] == "handoff_failed"
        assert final_call["handoff"]["error"] == (
            "customer call ended before handoff connected"
        )
        assert final_call["handoff"]["can_claim"] is False
        assert ("hangup", customer_call_id, "NORMAL_CLEARING") in operations
        assert ("hangup", "agent-uuid-1", "NORMAL_CLEARING") in operations
        assert ("break_audio_stream", customer_call_id, None) not in operations
        assert ("bridge", customer_call_id, "agent-uuid-1") not in operations
    finally:
        manager.shutdown()


def test_outbound_manager_hangs_up_customer_when_failed_claim_expires_wait():
    operations: list[tuple[str, str, str]] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "sofia/internal/sip:agent@browser.invalid;transport=ws"

        async def originate(self, command: str) -> str:
            if "&park()" in command:
                await asyncio.sleep(1.05)
                return "-ERR USER_NOT_REGISTERED"
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            operations.append(("hangup", call_id, cause))
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {"last_utterance": "我要转人工", "wait_timeout_seconds": 1},
        )

        with pytest.raises(CallControlError):
            manager.claim_handoff(
                call_id,
                {"agent_extension": "1001", "agent_uuid": "agent-uuid-1"},
            )
        expired_call = _wait_for_status(manager, call_id, "hangup_sent")

        assert expired_call["handoff"]["state"] == "handoff_failed"
        assert expired_call["handoff"]["error"] == "-ERR USER_NOT_REGISTERED"
        assert operations == [("hangup", call_id, "NORMAL_CLEARING")]
    finally:
        manager.shutdown()


def test_outbound_manager_rejects_handoff_transcript_without_human_bridge():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return "user/1000"

        async def originate(self, command: str) -> str:
            return "+OK customer-call"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")
        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        manager.request_handoff(
            call_id,
            {
                "trigger": "customer_requested",
                "reason": "request_human",
                "last_utterance": "我要转人工",
            },
        )
        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
            )
        )

        with pytest.raises(CallControlError) as exc_info:
            manager.complete_handoff_transcript(
                call_id,
                {"turns": [{"role": "assistant", "text": "您好，我是物业客服。"}]},
            )

        assert exc_info.value.status_code == 409
        assert "human handoff is not active" in str(exc_info.value)
    finally:
        manager.shutdown()


def test_call_record_exposes_busy_diagnostics():
    record = OutboundCallRecord(
        call_id="call-1",
        destination="1000",
        endpoint="sofia/internal/sip:1000@127.0.0.1:5060",
        requested_endpoint="sofia_contact:*/1000",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="AI_Assistant",
        caller_id_number="9000",
        originate_timeout_seconds=30,
        status="failed",
        created_at_ms=1000,
        started_at_ms=1200,
        completed_at_ms=6800,
        freeswitch_reply="-ERR USER_BUSY",
        error="-ERR USER_BUSY",
    )

    payload = record.to_dict()

    assert payload["phase"] == "busy"
    assert payload["phase_label"] == "忙线/拒接"
    assert payload["failure_reason"] == "USER_BUSY"
    assert payload["failure_label"] == "对端忙线或拒接"
    assert payload["sip_status_hint"] == "486"
    assert payload["elapsed_ms"] == 5600


def test_call_record_maps_sip_provider_508_upstream_failure():
    record = OutboundCallRecord(
        call_id="call-1",
        destination="15800967789",
        endpoint="sofia/gateway/sip-provider/15800967789",
        requested_endpoint="sofia/gateway/sip-provider/15800967789",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="037123124810",
        caller_id_number="037123124810",
        originate_timeout_seconds=30,
        status="failed",
        created_at_ms=1000,
        started_at_ms=1200,
        completed_at_ms=6800,
        hangup_cause="NORMAL_UNSPECIFIED",
        sip_status="508",
        sip_reason="31",
    )

    payload = record.to_dict()

    assert payload["phase"] == "trunk_or_upstream_failure"
    assert payload["phase_label"] == "线路或上游失败"
    assert payload["failure_reason"] == "NORMAL_UNSPECIFIED"
    assert payload["failure_label"] == "线路或上游未明原因失败"
    assert payload["sip_status_hint"] == "508"


def test_call_record_maps_sip_provider_508_without_hangup_cause():
    record = OutboundCallRecord(
        call_id="call-1",
        destination="15800967789",
        endpoint="sofia/gateway/sip-provider/15800967789",
        requested_endpoint="sofia/gateway/sip-provider/15800967789",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="037123124810",
        caller_id_number="037123124810",
        originate_timeout_seconds=30,
        status="failed",
        created_at_ms=1000,
        started_at_ms=1200,
        completed_at_ms=6800,
        sip_status="508",
    )

    payload = record.to_dict()

    assert payload["phase"] == "trunk_or_upstream_failure"
    assert payload["failure_reason"] == "SIP_508"
    assert payload["failure_label"] == "线路或上游未明原因失败"


def test_call_record_maps_sip_408_timer_expire_to_no_answer():
    record = OutboundCallRecord(
        call_id="call-1",
        destination="19900000000",
        endpoint="sofia/gateway/sip-provider-sandbox/19900000000",
        requested_endpoint="sofia/gateway/sip-provider-sandbox/19900000000",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="037123124810",
        caller_id_number="037123124810",
        originate_timeout_seconds=8,
        status="failed",
        created_at_ms=1000,
        started_at_ms=1200,
        ringing_at_ms=1300,
        completed_at_ms=6800,
        hangup_cause="RECOVERY_ON_TIMER_EXPIRE",
        sip_status="408",
        sip_reason="102",
    )

    payload = record.to_dict()

    assert payload["phase"] == "no_answer"
    assert payload["phase_label"] == "无人接听"
    assert payload["failure_reason"] == "NO_ANSWER"
    assert payload["failure_label"] == "无人接听"


def test_call_record_does_not_treat_success_reply_as_failure():
    record = OutboundCallRecord(
        call_id="call-1",
        destination="1000",
        endpoint="sofia/internal/sip:1000@127.0.0.1:5060",
        requested_endpoint="sofia_contact:*/1000",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="AI_Assistant",
        caller_id_number="9000",
        originate_timeout_seconds=30,
        status="originated",
        created_at_ms=1000,
        started_at_ms=1200,
        completed_at_ms=3200,
        freeswitch_reply="+OK call-1",
        error=None,
    )

    payload = record.to_dict()

    assert payload["phase"] == "answered"
    assert payload["phase_label"] == "已接通"
    assert payload["failure_reason"] is None
    assert payload["failure_label"] is None
    assert payload["failure_hint"] is None
    assert payload["elapsed_ms"] == 2000


def test_call_record_exposes_completed_timing_from_channel_events():
    record = OutboundCallRecord(
        call_id="call-1",
        destination="1000",
        endpoint="sofia/internal/sip:1000@127.0.0.1:5060",
        requested_endpoint="sofia_contact:*/1000",
        dialplan_extension="9199",
        dialplan_context="default",
        caller_id_name="AI_Assistant",
        caller_id_number="9000",
        originate_timeout_seconds=30,
        status="completed",
        created_at_ms=1000,
        started_at_ms=1200,
        ringing_at_ms=1600,
        answered_at_ms=3200,
        completed_at_ms=8200,
        hangup_cause="NORMAL_CLEARING",
    )

    payload = record.to_dict()

    assert payload["phase"] == "completed"
    assert payload["phase_label"] == "已结束"
    assert payload["failure_reason"] is None
    assert payload["elapsed_ms"] == 7000
    assert payload["ringing_ms"] == 1600
    assert payload["talk_duration_ms"] == 5000
    assert payload["failure_label"] is None


def test_parse_create_call_rejects_unsafe_destination():
    with pytest.raises(CallControlError, match="destination"):
        parse_create_call_request({"destination": "1000 9199"})


def test_parse_create_call_rejects_unsafe_caller_name():
    with pytest.raises(CallControlError, match="caller_id_name"):
        parse_create_call_request(
            {
                "destination": "1000",
                "caller_id_name": "AI Assistant",
            }
        )


def test_parse_create_call_accepts_java_ai_call_trigger_shape():
    request = parse_create_call_request(
        {
            "schemaVersion": "1.0",
            "tenantId": "100001",
            "taskId": "2050000000000100001",
            "callId": "2050000000000100001",
            "nodeCode": "ai_call",
            "identityName": "项目员工",
            "debtId": "2050000000000200001",
            "destination": "15800967789",
            "params": {"timeoutMinutes": 30},
            "flowContext": {"schemaVersion": "1.0"},
        }
    )

    assert request.destination == "15800967789"
    assert request.external_call_id == "2050000000000100001"
    assert request.context == {
        "tenantId": "100001",
        "taskId": "2050000000000100001",
        "callId": "2050000000000100001",
        "nodeCode": "ai_call",
        "identityName": "项目员工",
        "debtId": "2050000000000200001",
    }


def test_parse_create_call_accepts_java_ai_call_trigger_without_destination():
    request = parse_create_call_request(
        {
            "tenantId": "100001",
            "taskId": "2050000000000100001",
            "callId": "2050000000000100001",
            "nodeCode": "ai_call",
            "identityName": "项目员工",
            "debtId": "2050000000000200001",
        }
    )

    assert request.destination is None
    assert request.context["debtId"] == "2050000000000200001"


def test_parse_create_call_rejects_local_placeholder_business_ids():
    with pytest.raises(CallControlError, match="real call_record"):
        parse_create_call_request(
            {
                "destination": "1000",
                "context": {
                    "callId": "handoff-local-20260602081728-4966",
                    "taskId": "handoff-local-20260602081728-4966",
                    "identityName": "项目员工",
                    "debtId": "2999000003846686611",
                },
            }
        )


def test_outbound_manager_originates_in_background():
    commands: list[str] = []
    call_record_events: list[tuple[str, dict]] = []

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            call_record_events.append(("started", context))

        def mark_failed(self, context):
            call_record_events.append(("failed", context))

        def mark_no_answer(self, context):
            call_record_events.append(("no_answer", context))

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            commands.append(command)
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(
        event_socket=EventSocketConfig(enabled=True),
        outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
    )
    manager = OutboundCallManager(
        config,
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "external_call_id": "biz-1",
                "caller_id_number": "9000",
                "context": {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                    "identityName": "项目员工",
                },
            }
        )

        assert call["status"] in {"queued", "originating", "originated"}
        final_call = _wait_for_status(manager, call["call_id"], "originated")
        assert final_call["freeswitch_reply"] == "+OK call accepted"
        assert commands
        assert "user/1000 9199 XML default" in commands[0]
        assert call_record_events == [
            (
                "started",
                {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                    "identityName": "项目员工",
                },
            )
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_resolves_destination_from_debt_id():
    commands: list[str] = []
    resolved_contexts: list[dict] = []

    class FakeDestinationResolver:
        def resolve(self, context):
            resolved_contexts.append(context)
            return "15800967789"

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            commands.append(command)
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="sofia/gateway/demo/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
        destination_resolver=FakeDestinationResolver(),
    )

    try:
        call = manager.create_call(
            {
                "tenantId": "100001",
                "taskId": "task-1",
                "callId": "990000000000032001",
                "nodeCode": "ai_call",
                "identityName": "项目员工",
                "debtId": "2049810626160668673",
            }
        )

        final_call = _wait_for_status(manager, call["call_id"], "originated")
        assert final_call["destination"] == "15800967789"
        assert "sofia/gateway/demo/15800967789 9199 XML default" in commands[0]
        assert resolved_contexts == [
            {
                "tenantId": "100001",
                "taskId": "task-1",
                "callId": "990000000000032001",
                "nodeCode": "ai_call",
                "identityName": "项目员工",
                "debtId": "2049810626160668673",
            }
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_rejects_missing_destination_when_phone_not_found():
    class EmptyDestinationResolver:
        def resolve(self, context):
            return None

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            raise AssertionError("call must be rejected before originate")

        async def originate(self, command: str) -> str:
            raise AssertionError("call must be rejected before originate")

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        destination_resolver=EmptyDestinationResolver(),
    )

    try:
        with pytest.raises(CallControlError, match="debtor phone") as err:
            manager.create_call(
                {
                    "tenantId": "100001",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "nodeCode": "ai_call",
                    "identityName": "项目员工",
                    "debtId": "2049810626160668673",
                }
            )

        assert err.value.status_code == 400
        assert manager.list_calls() == []
    finally:
        manager.shutdown()


def test_outbound_manager_emits_accepted_flow_callback_after_call_is_queued():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(
        event_socket=EventSocketConfig(enabled=True),
        outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
    )
    manager = OutboundCallManager(
        config,
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "external_call_id": "biz-call-1",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )

        assert flow_events[0].status == "ACCEPTED"
        assert flow_events[0].tenant_id == "000000"
        assert flow_events[0].task_id == "task-1"
        assert flow_events[0].business_id == "biz-call-1"
        assert flow_events[0].message == "外呼任务已受理"
        assert call["status"] in {"queued", "originating", "originated"}
    finally:
        manager.shutdown()


def test_outbound_manager_uses_java_call_id_and_is_idempotent_for_retries():
    originate_count = 0
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            nonlocal originate_count
            originate_count += 1
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    payload = {
        "tenantId": "100001",
        "taskId": "2050000000000100001",
        "callId": "2050000000000100001",
        "nodeCode": "ai_call",
        "identityName": "项目员工",
        "debtId": "2050000000000200001",
        "destination": "15800967789",
    }
    try:
        first = manager.create_call(payload)
        second = manager.create_call(payload)

        assert first["external_call_id"] == "2050000000000100001"
        assert second["call_id"] == first["call_id"]
        _wait_for_status(manager, first["call_id"], "originated")
        assert originate_count == 1
        assert [event.status for event in flow_events] == ["ACCEPTED"]
        assert flow_events[0].business_id == "2050000000000100001"
    finally:
        manager.shutdown()


def test_outbound_manager_skips_flow_callback_without_task_id():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )

        assert flow_events == []
    finally:
        manager.shutdown()


def test_outbound_manager_requires_task_id_when_flow_callback_enabled():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            raise AssertionError("call must be rejected before originate")

        async def originate(self, command: str) -> str:
            raise AssertionError("call must be rejected before originate")

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            flow_callback=FlowCallbackConfig(enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        with pytest.raises(CallControlError, match="context.taskId") as err:
            manager.create_call(
                {
                    "destination": "1000",
                    "context": {
                        "tenantId": "000000",
                        "callId": "990000000000032001",
                        "debtId": "2049810626160668673",
                    },
                }
            )

        assert err.value.status_code == 400
        assert manager.list_calls() == []
    finally:
        manager.shutdown()


def test_outbound_manager_rejects_flow_callback_call_without_persistence_wiring():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            raise AssertionError("call must be rejected before originate")

        async def originate(self, command: str) -> str:
            raise AssertionError("call must be rejected before originate")

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            flow_callback=FlowCallbackConfig(enabled=True),
        ),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        with pytest.raises(CallControlError, match="call_record persistence") as err:
            manager.create_call(
                {
                    "destination": "1000",
                    "context": {
                        "tenantId": "000000",
                        "taskId": "task-1",
                        "callId": "990000000000032001",
                        "debtId": "2049810626160668673",
                    },
                }
            )

        assert err.value.status_code == 503
        assert manager.list_calls() == []
        assert flow_events == []
    finally:
        manager.shutdown()


def test_outbound_manager_syncs_call_record_failed_when_originate_fails():
    call_record_events: list[tuple[str, dict]] = []

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            call_record_events.append(("started", context))

        def mark_failed(self, context):
            call_record_events.append(("failed", context))

        def mark_no_answer(self, context):
            call_record_events.append(("no_answer", context))

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "-ERR USER_BUSY"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(
        event_socket=EventSocketConfig(enabled=True),
        outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
    )
    manager = OutboundCallManager(
        config,
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )

        _wait_for_status(manager, call["call_id"], "failed")
        assert call_record_events == [
            (
                "started",
                {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            ),
            (
                "failed",
                {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            ),
        ]
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_flow_callback_after_call_record_failure_sync():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            return True

        def mark_failed(self, context):
            return True

        def mark_no_answer(self, context):
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "-ERR USER_BUSY"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(
        event_socket=EventSocketConfig(enabled=True),
        outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
    )
    manager = OutboundCallManager(
        config,
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )

        _wait_for_status(manager, call["call_id"], "failed")
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-1"
        assert flow_events[-1].business_id == "990000000000032001"
        assert flow_events[-1].message == "外呼失败"
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_flow_callback_for_no_answer_event():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            return True

        def mark_failed(self, context):
            return True

        def mark_no_answer(self, context):
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-no-answer",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NO_ANSWER",
            )
        )

        assert manager.get_call(call_id)["status"] == "no_answer"
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-no-answer"
        assert flow_events[-1].message == "外呼失败"
    finally:
        manager.shutdown()


def test_outbound_manager_emits_failed_flow_callback_for_busy_event():
    flow_events: list[FlowCallbackEvent] = []

    class FakeFlowCallbackWriter:
        def publish(self, event):
            flow_events.append(event)
            return True

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            return True

        def mark_failed(self, context):
            return True

        def mark_no_answer(self, context):
            return True

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
        flow_callback_writer=FakeFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-busy",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="USER_BUSY",
                sip_status="486",
            )
        )

        assert manager.get_call(call_id)["status"] == "busy"
        assert [event.status for event in flow_events] == ["ACCEPTED", "FAILED"]
        assert flow_events[-1].task_id == "task-busy"
        assert flow_events[-1].business_id == "990000000000032001"
    finally:
        manager.shutdown()


def test_outbound_manager_ignores_flow_callback_writer_failure():
    class BrokenFlowCallbackWriter:
        def publish(self, event):
            raise OSError("mq unavailable")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        flow_callback_writer=BrokenFlowCallbackWriter(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )

        assert _wait_for_status(manager, call["call_id"], "originated")["status"] == (
            "originated"
        )
    finally:
        manager.shutdown()


def test_outbound_manager_generates_opening_before_originating():
    events: list[str] = []
    commands: list[str] = []
    store = OpeningAudioStore()

    class FakeOpeningGenerator:
        def generate(self, opening):
            events.append(f"generate:{opening.voice}:{opening.opening_text_hash}")
            return OpeningAudio(
                pcm16=samples_to_pcm_s16le([1200] * 480),
                sample_rate=24000,
                generation_ms=1200,
            )

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            events.append("originate")
            commands.append(command)
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(
        event_socket=EventSocketConfig(enabled=True),
        outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
    )
    manager = OutboundCallManager(
        config,
        dialer_factory=lambda: FakeDialer(),
        opening_generator=FakeOpeningGenerator(),
        opening_store=store,
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "caller_id_number": "9000",
                "opening": {
                    "voice": "female",
                    "business": {
                        "owner_name": "测试业主",
                        "arrears_amount": "12.34",
                    },
                },
            }
        )

        assert call["status"] in {"queued", "originating", "originated"}
        assert call["opening"]["status"] == "ready"
        assert call["opening"]["voice"] == "female"
        assert call["opening"]["audio_sample_rate"] == 24000
        assert call["opening"]["generation_ms"] == 1200
        assert "opening_text" not in call["opening"]
        assert call["context"] == {}
        final_call = _wait_for_status(manager, call["call_id"], "originated")
        assert final_call["opening"]["call_started_after_opening_ready"] is True
        assert store.pop(call["call_id"]) is not None
        assert events[0].startswith("generate:female:")
        assert events[-1] == "originate"
        assert commands
    finally:
        manager.shutdown()


def test_outbound_manager_prepares_business_prompt_and_opening_before_originating():
    events: list[str] = []
    store = OpeningAudioStore()
    snapshot = PromptSnapshot(
        scene="collector-a:persona-1",
        version="postgres",
        instructions="业务提示词",
        content_hash="hash-prompt",
        loaded_at_ms=123,
        metadata={"source": "postgres"},
    )

    class FakeBusinessPromptPreparer:
        def prepare(self, context):
            events.append("prepare_prompt")
            assert context == {
                "identityName": "collector-a",
                "personaId": "persona-1",
                "debtId": "debt-1",
            }
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
            return BusinessPromptPreparation(snapshot, opening)

    class FakeOpeningGenerator:
        def generate(self, opening):
            events.append("generate_opening")
            assert opening.opening_text_hash
            return OpeningAudio(
                pcm16=samples_to_pcm_s16le([1200] * 480),
                sample_rate=24000,
                generation_ms=1200,
            )

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            events.append("originate")
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        opening_generator=FakeOpeningGenerator(),
        opening_store=store,
        business_prompt_preparer=FakeBusinessPromptPreparer(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "identityName": "collector-a",
                    "personaId": "persona-1",
                    "debtId": "debt-1",
                },
            }
        )

        assert call["prompt"]["content_hash"] == "hash-prompt"
        assert "instructions" not in call["prompt"]
        assert call["opening"]["status"] == "ready"
        assert manager.get_prompt_snapshot(call["call_id"]) is snapshot
        _wait_for_status(manager, call["call_id"], "originated")
        assert events == ["prepare_prompt", "generate_opening", "originate"]
    finally:
        manager.shutdown()


def test_outbound_manager_does_not_originate_when_opening_generation_fails():
    commands: list[str] = []

    class FakeOpeningGenerator:
        def generate(self, opening):
            raise OpeningGenerationFailed("opening_generation_failed")

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            commands.append(command)
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        opening_generator=FakeOpeningGenerator(),
        opening_store=OpeningAudioStore(),
    )

    try:
        with pytest.raises(CallControlError, match="opening_generation_failed") as err:
            manager.create_call(
                {
                    "destination": "1000",
                    "opening": {
                        "business": {
                            "owner_name": "测试业主",
                            "arrears_amount": "12.34",
                        },
                    },
                }
            )
        assert err.value.status_code == 502
        assert commands == []
        assert manager.list_calls() == []
    finally:
        manager.shutdown()


def test_outbound_manager_maps_opening_generation_timeout_to_504():
    class FakeOpeningGenerator:
        def generate(self, opening):
            raise OpeningGenerationTimeout("opening_generation_timeout")

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        opening_generator=FakeOpeningGenerator(),
        opening_store=OpeningAudioStore(),
    )

    try:
        with pytest.raises(CallControlError, match="opening_generation_timeout") as err:
            manager.create_call(
                {
                    "destination": "1000",
                    "opening": {
                        "business": {
                            "owner_name": "测试业主",
                            "arrears_amount": "12.34",
                        },
                    },
                }
            )
        assert err.value.status_code == 504
    finally:
        manager.shutdown()


def test_outbound_manager_requires_event_socket_enabled():
    config = GatewayConfig(event_socket=EventSocketConfig(enabled=False))
    manager = OutboundCallManager(config)

    try:
        with pytest.raises(CallControlError, match="Event Socket"):
            manager.create_call({"destination": "1000"})
    finally:
        manager.shutdown()


def test_outbound_manager_resolves_sofia_contact_endpoint():
    commands: list[str] = []

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            assert endpoint == "sofia_contact:*/1000"
            return "sofia/internal/sip:1000@127.0.0.1:5060"

        async def originate(self, command: str) -> str:
            commands.append(command)
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(
        event_socket=EventSocketConfig(enabled=True),
        outbound=OutboundCallConfig(endpoint_template="sofia_contact:*/{destination}"),
    )
    manager = OutboundCallManager(config, dialer_factory=lambda: FakeDialer())

    try:
        call = manager.create_call({"destination": "1000"})
        final_call = _wait_for_status(manager, call["call_id"], "originated")
        assert final_call["requested_endpoint"] == "sofia_contact:*/1000"
        assert final_call["endpoint"] == "sofia/internal/sip:1000@127.0.0.1:5060"
        assert "sofia/internal/sip:1000@127.0.0.1:5060 9199 XML default" in commands[0]
    finally:
        manager.shutdown()


def test_outbound_manager_uses_distinct_default_local_caller():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(event_socket=EventSocketConfig(enabled=True))
    manager = OutboundCallManager(config, dialer_factory=lambda: FakeDialer())

    try:
        call = manager.create_call({"destination": "1000"})
        final_call = _wait_for_status(manager, call["call_id"], "originated")
        assert final_call["caller_id_number"] == "9000"
    finally:
        manager.shutdown()


def test_outbound_manager_applies_channel_state_events():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(event_socket=EventSocketConfig(enabled=True))
    manager = OutboundCallManager(config, dialer_factory=lambda: FakeDialer())

    try:
        call = manager.create_call({"destination": "1000"})
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")

        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_PROGRESS", call_id=call_id)
        )
        assert manager.get_call(call_id)["status"] == "ringing"

        manager.handle_channel_event(
            ChannelStateEvent(name="CHANNEL_ANSWER", call_id=call_id)
        )
        assert manager.get_call(call_id)["status"] == "answered"

        manager.mark_media_connected(call_id)
        assert manager.get_call(call_id)["status"] == "media_connected"

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NORMAL_CLEARING",
                sip_status="200",
            )
        )
        final_call = manager.get_call(call_id)
        assert final_call["status"] == "completed"
        assert final_call["phase"] == "completed"
        assert final_call["hangup_cause"] == "NORMAL_CLEARING"
        assert final_call["sip_status"] == "200"
        assert final_call["talk_duration_ms"] is not None
    finally:
        manager.shutdown()


def test_outbound_manager_maps_unanswered_hangup_event():
    call_record_events: list[tuple[str, dict]] = []

    class FakeCallRecordUpdater:
        def mark_started(self, context):
            call_record_events.append(("started", context))

        def mark_failed(self, context):
            call_record_events.append(("failed", context))

        def mark_no_answer(self, context):
            call_record_events.append(("no_answer", context))

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    config = GatewayConfig(event_socket=EventSocketConfig(enabled=True))
    manager = OutboundCallManager(
        config,
        dialer_factory=lambda: FakeDialer(),
        call_record_updater=FakeCallRecordUpdater(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            }
        )
        call_id = call["call_id"]
        _wait_for_status(manager, call_id, "originated")

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=call_id,
                hangup_cause="NO_ANSWER",
            )
        )
        final_call = manager.get_call(call_id)
        assert final_call["status"] == "no_answer"
        assert final_call["phase"] == "no_answer"
        assert final_call["failure_label"] == "无人接听"
        assert call_record_events == [
            (
                "started",
                {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            ),
            (
                "no_answer",
                {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            ),
        ]
    finally:
        manager.shutdown()


def _wait_for_status(
    manager: OutboundCallManager,
    call_id: str,
    status: str,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        call = manager.get_call(call_id)
        if call is not None and call["status"] == status:
            return call
        time.sleep(0.02)
    raise AssertionError(f"call {call_id} did not reach {status}")


def _wait_for_handoff_recording_status(
    manager: OutboundCallManager,
    call_id: str,
    status: str,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        call = manager.get_call(call_id)
        if (
            call is not None
            and call.get("handoff") is not None
            and call["handoff"].get("recording_status") == status
        ):
            return call
        time.sleep(0.02)
    raise AssertionError(f"call {call_id} recording did not reach {status}")


def _wait_for_handoff_transcript_status(
    manager: OutboundCallManager,
    call_id: str,
    status: str,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        call = manager.get_call(call_id)
        if (
            call is not None
            and call.get("handoff") is not None
            and call["handoff"].get("human_transcript_status") == status
        ):
            return call
        time.sleep(0.02)
    raise AssertionError(f"call {call_id} transcript did not reach {status}")


def _wait_for_writer_event_count(events: list, count: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if len(events) >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"writer events did not reach {count}: {events!r}")


def _wait_for_flow_event_count(events: list, count: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if len(events) >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"flow events did not reach {count}: {events!r}")
