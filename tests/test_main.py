from __future__ import annotations

from app.config import GatewayConfig, HumanTranscriptConfig
from app.handoff_transcript import MockHumanHandoffTranscriptProcessor
from app.main import (
    DOUBAO_DIALOG_FIELD_COMPAT_SYSTEM_PROMPT,
    _browser_first_prompt_snapshot_provider,
    _build_handoff_transcript_processor,
    _system_prompt_for_doubao_session,
)
from app.postgres import PromptSnapshot
from app.realtime_types import RealtimeDialogConfig


def test_system_prompt_keeps_default_instructions_without_dialog_role():
    assert (
        _system_prompt_for_doubao_session("默认电话提示词", RealtimeDialogConfig())
        == "默认电话提示词"
    )


def test_system_prompt_uses_compat_prompt_when_dialog_role_carries_business_prompt():
    result = _system_prompt_for_doubao_session(
        "完整业务提示词",
        RealtimeDialogConfig(system_role="你是物业中心小明。\n# 业务提示词\n完整业务提示词"),
    )

    assert result == DOUBAO_DIALOG_FIELD_COMPAT_SYSTEM_PROMPT
    assert "完整业务提示词" not in result


def test_browser_first_prompt_snapshot_provider_prefers_browser_store():
    browser_snapshot = PromptSnapshot(
        scene="browser-realtime-test",
        version="browser-test",
        instructions="browser prompt",
        content_hash="browser-hash",
        loaded_at_ms=1,
        metadata={"source": "browser-realtime-test"},
    )
    outbound_snapshot = PromptSnapshot(
        scene="default",
        version="postgres",
        instructions="outbound prompt",
        content_hash="outbound-hash",
        loaded_at_ms=2,
        metadata={"source": "postgres"},
    )

    class Store:
        def get(self, call_id):
            return browser_snapshot if call_id == "browser-1" else None

    def outbound_provider(call_id):
        return outbound_snapshot if call_id == "real-1" else None

    provider = _browser_first_prompt_snapshot_provider(Store(), outbound_provider)

    assert provider("browser-1") is browser_snapshot
    assert provider("real-1") is outbound_snapshot
    assert provider("missing") is None


def test_build_handoff_transcript_processor_supports_mock_provider():
    processor = _build_handoff_transcript_processor(
        GatewayConfig(
            human_transcript=HumanTranscriptConfig(
                enabled=True,
                provider="mock",
            )
        )
    )

    assert isinstance(processor, MockHumanHandoffTranscriptProcessor)
