from __future__ import annotations

import pytest

from app.config import GatewayConfig, HumanTranscriptConfig, QwenOmniRealtimeConfig
from app.handoff_transcript import MockHumanHandoffTranscriptProcessor
from app.main import (
    DOUBAO_DIALOG_FIELD_COMPAT_SYSTEM_PROMPT,
    _browser_first_prompt_snapshot_provider,
    _build_handoff_transcript_processor,
    _load_qwen_omni_realtime_credentials,
    _qwen_instructions_for_session,
    _system_prompt_for_doubao_session,
)
from app.postgres import PromptSnapshot
from app.realtime_types import RealtimeDialogConfig, RealtimeDialogContextItem


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


def test_load_qwen_omni_realtime_credentials_reads_configured_env(monkeypatch):
    monkeypatch.setenv("TEST_DASHSCOPE_API_KEY", "sk-test")

    credentials = _load_qwen_omni_realtime_credentials(
        GatewayConfig(
            qwen_omni_realtime=QwenOmniRealtimeConfig(
                api_key_env="TEST_DASHSCOPE_API_KEY",
                websocket_url="wss://example.test/qwen",
                model="qwen3-omni-flash-realtime",
            )
        )
    )

    assert credentials.api_key == "sk-test"
    assert credentials.websocket_url == "wss://example.test/qwen"
    assert credentials.model == "qwen3-omni-flash-realtime"


def test_load_qwen_omni_realtime_credentials_reports_missing_env(monkeypatch):
    monkeypatch.delenv("TEST_DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="TEST_DASHSCOPE_API_KEY"):
        _load_qwen_omni_realtime_credentials(
            GatewayConfig(
                qwen_omni_realtime=QwenOmniRealtimeConfig(
                    api_key_env="TEST_DASHSCOPE_API_KEY",
                )
            )
        )


def test_qwen_instructions_include_dialog_fields_and_history():
    instructions = _qwen_instructions_for_session(
        "默认提示词",
        RealtimeDialogConfig(
            bot_name="小林",
            system_role="你是物业中心客服。",
            speaking_style="语气简短自然。",
            dialog_context=(
                RealtimeDialogContextItem(role="assistant", text="您好，请问方便沟通吗？"),
                RealtimeDialogContextItem(role="user", text="可以。"),
            ),
        ),
    )

    assert "默认提示词" in instructions
    assert "bot_name: 小林" in instructions
    assert "system_role: 你是物业中心客服。" in instructions
    assert "speaking_style: 语气简短自然。" in instructions
    assert "assistant: 您好，请问方便沟通吗？" in instructions
    assert "user: 可以。" in instructions
