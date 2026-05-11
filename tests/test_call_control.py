from __future__ import annotations

import time

import pytest

from app.call_control import (
    CallControlError,
    OutboundCallManager,
    OutboundCallRecord,
    build_originate_command,
    parse_create_call_request,
)
from app.config import EventSocketConfig, GatewayConfig, OutboundCallConfig
from app.freeswitch_event_socket import ChannelStateEvent


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
    assert "sip_realtime_external_call_id=biz-1" in command
    assert command.endswith("}user/1000 9199 XML default")


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


def test_outbound_manager_originates_in_background():
    commands: list[str] = []

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
    manager = OutboundCallManager(config, dialer_factory=lambda: FakeDialer())

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "external_call_id": "biz-1",
                "caller_id_number": "9000",
            }
        )

        assert call["status"] == "queued"
        final_call = _wait_for_status(manager, call["call_id"], "originated")
        assert final_call["freeswitch_reply"] == "+OK call accepted"
        assert commands
        assert "user/1000 9199 XML default" in commands[0]
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
