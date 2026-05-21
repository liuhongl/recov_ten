from __future__ import annotations

from app.main import (
    DOUBAO_DIALOG_FIELD_COMPAT_SYSTEM_PROMPT,
    _system_prompt_for_doubao_session,
)
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
