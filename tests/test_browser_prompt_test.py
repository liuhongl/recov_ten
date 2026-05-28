from __future__ import annotations

import asyncio

from app.browser_prompt_test import (
    BrowserPromptTestStore,
    build_browser_prompt_snapshot,
)
from app.opening import OpeningAudio, OpeningAudioStore
from app.postgres import BusinessPromptPreparation, PostgresPromptStore, PromptSnapshot


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class SyncBusinessPromptPreparer:
    def __init__(self, store):
        self.store = store

    def prepare(self, context):
        return asyncio.run(
            self.store.prepare_business_prompt(
                context,
                fallback_instructions="fallback",
            )
        )


def test_manual_browser_prompt_uses_current_business_rules_without_copied_defaults():
    snapshot = build_browser_prompt_snapshot(
        {
            "call_id": "browser-manual-rules",
            "mode": "manual",
            "employee_name": "测试员工",
            "identityName": "项目员工",
            "strategy_core": "先确认本人，再说明费用。",
            "debt_amount": "12.34",
            "debtor_name": "金阳",
            "debtor_gender": "女",
            "sections": {
                "dialog_style": ["本次浏览器测试中，回答前先停顿半秒。"],
                "communication_norms": ["本次浏览器测试中，收口句保持简短。"],
            },
        }
    )

    assert snapshot.scene == "browser-realtime-test"
    assert snapshot.version == "browser-test"
    assert snapshot.metadata["source"] == "browser-realtime-test"
    assert snapshot.metadata["mode"] == "manual"
    assert "# 客服语气配置" in snapshot.instructions
    assert "电话客服口吻，简短、自然、礼貌但坚定" in snapshot.instructions
    assert "以已播放开场白为语气参照" not in snapshot.instructions
    assert "测试规则优先级。" not in snapshot.instructions
    assert "本次浏览器测试 speaking_style 覆盖" not in snapshot.instructions
    assert "# 浏览器测试覆盖规则" not in snapshot.instructions
    assert "# 浏览器测试补充规则" not in snapshot.instructions
    assert "# 对话风格\n1. 本次浏览器测试中，回答前先停顿半秒。" in snapshot.instructions
    assert "# 沟通规范\n1. 本次浏览器测试中，收口句保持简短。" in snapshot.instructions
    dialog_style_block = snapshot.instructions.split("# 对话风格", 1)[1].split(
        "\n\n# ",
        1,
    )[0]
    assert "数据库催收策略决定业务目标、推进方向和可表达的信息范围" not in dialog_style_block
    assert "客服语气配置决定表达风格、正式程度和语气强弱" not in dialog_style_block
    assert "12.34元" not in snapshot.instructions
    assert "系统记录待处理金额：12.34" not in snapshot.instructions
    assert "无论身份是否确认，均不得在通话中说出具体金额" in snapshot.instructions


def test_manual_browser_prompt_redacts_amounts_from_browser_overrides():
    snapshot = build_browser_prompt_snapshot(
        {
            "call_id": "browser-amount-redaction",
            "mode": "manual",
            "strategy_core": "浏览器策略里写了12.34元。",
            "speaking_style": "口吻里也不要说5200.75元。",
            "sections": {
                "extra": ["补充规则不要复述88.8元。"],
            },
        }
    )

    assert "12.34元" not in snapshot.instructions
    assert "5200.75元" not in snapshot.instructions
    assert "88.8元" not in snapshot.instructions
    assert snapshot.instructions.count("[金额已隐藏]") == 3


def test_manual_browser_prompt_sanitizes_legal_pressure_overrides():
    snapshot = build_browser_prompt_snapshot(
        {
            "call_id": "browser-legal-pressure",
            "mode": "manual",
            "identityName": "企业法务",
            "strategy_core": (
                "确认身份后做正式催告，说明账款已经进入法律跟进阶段。"
                "记录显示客户曾于X日、X日两次承诺缴纳但未实际到账。"
                "告知客户后续可能会面临诉讼。"
                "正常保留：先确认身份，再说明费用事项需要核实。"
            ),
            "speaking_style": "用正式催告口吻提醒客户尽快处理。",
            "sections": {
                "extra": ["补充规则：可以提示用户后续可能会面临诉讼。"],
            },
        }
    )

    strategy_block = snapshot.instructions.split("# 客服语气配置", 1)[0]
    override_parts = snapshot.instructions.split("# 浏览器测试覆盖规则", 1)
    override_block = override_parts[1] if len(override_parts) == 2 else ""
    for forbidden in (
        "确认身份后做正式催告",
        "账款已经进入法律跟进阶段",
        "客户曾于X日",
        "两次承诺缴纳",
        "告知客户后续可能会面临诉讼",
        "补充规则：可以提示用户后续可能会面临诉讼",
        "尽快处理",
    ):
        assert forbidden not in strategy_block
        assert forbidden not in override_block
        assert forbidden not in snapshot.metadata["speaking_style"]
    assert "正常保留：先确认身份" in snapshot.instructions
    assert "不得使用正式催告、法律跟进阶段、可能面临诉讼等法律施压表达" in snapshot.instructions


def test_database_browser_prompt_reuses_postgres_composition_and_sensitive_summary():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                assert args == ("collector-a", "000000", "女")
                return None
            if "from call_identity_name" in query:
                assert args == ("collector-a",)
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                assert args == ("collector-a", 3)
                return {
                    "strategy_core": "先确认本人，再说明费用。",
                    "speaking_style": "正式但亲切的客服口吻。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
                assert args == (2049810626160668673,)
                return {
                    "debtor_name": "金阳",
                    "address": "测试小区一号楼",
                    "debt_amount": "12.34",
                    "debtor_gender": "女",
                    "debtor_age": 38,
                    "tenant_id": "000000",
                    "persona_id": 3,
                }
            raise AssertionError(query)

        async def fetch(self, query, *args):
            return []

    store = BrowserPromptTestStore(
        business_prompt_preparer=SyncBusinessPromptPreparer(
            PostgresPromptStore(FakePool(Conn()))
        ),
        now=lambda: 100.0,
    )

    registration = store.register(
        {
            "call_id": "browser-database-prompt",
            "mode": "database",
            "context": {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
                "identityName": "collector-a",
                "personaId": "3",
            },
            "sections": {
                "privacy_disclosure": ["浏览器测试：身份未确认时仍然不能说金额。"],
            },
        }
    )

    snapshot = registration.prompt_snapshot
    assert store.get("browser-database-prompt") is snapshot
    assert snapshot.version == "browser-test"
    assert snapshot.metadata["mode"] == "database"
    assert snapshot.metadata["base_version"] == "postgres"
    assert snapshot.metadata["personaId"] == "3"
    assert snapshot.metadata["personaId_input"] == "3"
    assert snapshot.metadata["personaId_matches"] is True
    assert "先确认本人，再说明费用。" in snapshot.instructions
    assert "# 客服语气配置" in snapshot.instructions
    assert "正式但亲切的客服口吻。" in snapshot.instructions
    assert "正式但亲切的客服口吻。" in snapshot.metadata["speaking_style"]
    assert "12.34元" not in snapshot.instructions
    assert "系统记录待处理金额：12.34" not in snapshot.instructions
    assert "测试小区一号楼" not in snapshot.instructions
    assert "无论身份是否确认，均不得在通话中说出具体金额" in snapshot.instructions
    assert "用户询问欠款金额、差多少钱或待处理金额时" in snapshot.instructions
    assert "浏览器测试：身份未确认时仍然不能说金额。" in snapshot.instructions
    assert registration.sensitive_summary == {
        "amount_in_prompt": False,
        "amount_disclosure_forbidden": True,
        "address_room_detail_excluded": True,
    }


def test_database_browser_prompt_reports_persona_id_mismatch_without_using_it_for_lookup():
    base_snapshot = PromptSnapshot(
        scene="collector-a:3",
        version="postgres",
        instructions="无论身份是否确认，均不得在通话中说出具体金额。",
        content_hash="base-hash",
        loaded_at_ms=1,
        metadata={
            "source": "postgres",
            "identityName": "collector-a",
            "personaId": "3",
            "debtId": "2049810626160668673",
            "speaking_style": "数据库语气",
        },
    )
    preparation = BusinessPromptPreparation(
        prompt_snapshot=base_snapshot,
        opening=None,
    )

    class Preparer:
        def prepare(self, context):
            assert context["personaId"] == "99"
            return preparation

    store = BrowserPromptTestStore(business_prompt_preparer=Preparer())
    registration = store.register(
        {
            "call_id": "browser-persona-mismatch",
            "mode": "database",
            "context": {
                "identityName": "collector-a",
                "debtId": "2049810626160668673",
                "personaId": "99",
            },
        }
    )

    assert registration.prompt_snapshot.metadata["personaId"] == "3"
    assert registration.prompt_snapshot.metadata["personaId_input"] == "99"
    assert registration.prompt_snapshot.metadata["personaId_matches"] is False
    assert registration.warnings == ["personaId 输入值 99 与数据库策略 personaId 3 不一致，已使用数据库值。"]


def test_browser_prompt_store_only_accepts_browser_call_ids_and_expires_items():
    current_time = 100.0

    def now() -> float:
        return current_time

    store = BrowserPromptTestStore(ttl_seconds=10, now=now)
    registration = store.register(
        {
            "call_id": "browser-expiring",
            "mode": "manual",
            "sections": {"extra": ["短期有效。"]},
        }
    )

    assert store.get("browser-expiring") is registration.prompt_snapshot
    assert store.get("real-call-id") is None

    try:
        store.register({"call_id": "real-call-id", "mode": "manual"})
    except ValueError as err:
        assert "browser-" in str(err)
    else:
        raise AssertionError("expected non-browser call id to be rejected")

    current_time = 111.0
    assert store.get("browser-expiring") is None


def test_sensitive_summary_flags_explicit_address_or_detail_overrides():
    store = BrowserPromptTestStore()

    registration = store.register(
        {
            "call_id": "browser-sensitive-override",
            "mode": "manual",
            "sections": {"extra": ["地址：测试小区一号楼"]},
        }
    )

    assert registration.sensitive_summary["address_room_detail_excluded"] is False


def test_database_browser_prompt_can_prepare_opening_audio_for_browser_call_id():
    opening_store = OpeningAudioStore()

    class Generator:
        def generate(self, opening):
            return OpeningAudio(
                pcm16=b"\x00\x00" * 160,
                sample_rate=8000,
                generation_ms=12,
            )

    base_snapshot = PromptSnapshot(
        scene="collector-a:3",
        version="postgres",
        instructions="无论身份是否确认，均不得在通话中说出具体金额。",
        content_hash="base-hash",
        loaded_at_ms=1,
        metadata={
            "source": "postgres",
            "identityName": "collector-a",
            "personaId": "3",
            "debtId": "2049810626160668673",
        },
    )

    class Preparer:
        def prepare(self, context):
            from app.opening import build_business_opening_request

            return BusinessPromptPreparation(
                prompt_snapshot=base_snapshot,
                opening=build_business_opening_request(
                    employee_name="李经理",
                    debtor_name="金阳",
                    debtor_gender="女",
                    debt_amount="12.34",
                    address="测试小区一号楼",
                    speaking_style="正式但亲切的客服口吻。",
                ),
            )

    store = BrowserPromptTestStore(
        business_prompt_preparer=Preparer(),
        opening_generator=Generator(),
        opening_store=opening_store,
    )

    registration = store.register(
        {
            "call_id": "browser-opening",
            "mode": "database",
            "context": {
                "identityName": "collector-a",
                "debtId": "2049810626160668673",
            },
            "opening": {"enabled": True},
        }
    )

    prepared = opening_store.pop("browser-opening")
    assert prepared is not None
    assert prepared.opening_text.startswith("您好，请问是金女士吗？我是李经理。")
    assert registration.opening is not None
    assert registration.opening["status"] == "ready"
    assert registration.opening["text_hash"] == prepared.opening_text_hash


def test_database_browser_prompt_speaking_style_override_replaces_prompt_and_opening_style():
    opening_store = OpeningAudioStore()
    captured = {}

    class Generator:
        def generate(self, opening):
            captured["speaking_style"] = opening.speaking_style
            return OpeningAudio(
                pcm16=b"\x00\x00" * 160,
                sample_rate=8000,
                generation_ms=12,
            )

    base_snapshot = PromptSnapshot(
        scene="collector-a:3",
        version="postgres",
        instructions=(
            "# 角色\n"
            "你是李经理。\n"
            "\n"
            "# 催收策略\n"
            "先确认本人，再说明费用。\n"
            "\n"
            "# 客服语气配置\n"
            "正式但亲切的客服口吻。\n"
            "\n"
            "# 规则优先级\n"
            "1. 不得突破法律红线。"
        ),
        content_hash="base-hash",
        loaded_at_ms=1,
        metadata={
            "source": "postgres",
            "identityName": "collector-a",
            "personaId": "3",
            "debtId": "2049810626160668673",
            "speaking_style": "正式但亲切的客服口吻。",
        },
    )

    class Preparer:
        def prepare(self, context):
            from app.opening import build_business_opening_request

            return BusinessPromptPreparation(
                prompt_snapshot=base_snapshot,
                opening=build_business_opening_request(
                    employee_name="李经理",
                    debtor_name="金阳",
                    debtor_gender="女",
                    debt_amount="12.34",
                    address="测试小区一号楼",
                    speaking_style="正式但亲切的客服口吻。",
                ),
            )

    store = BrowserPromptTestStore(
        business_prompt_preparer=Preparer(),
        opening_generator=Generator(),
        opening_store=opening_store,
    )

    registration = store.register(
        {
            "call_id": "browser-style-override",
            "mode": "database",
            "context": {
                "identityName": "collector-a",
                "debtId": "2049810626160668673",
            },
            "overrides": {"speaking_style": "你口吻拽拽的。"},
            "opening": {"enabled": True},
        }
    )

    snapshot = registration.prompt_snapshot
    assert snapshot.metadata["speaking_style"] == "你口吻拽拽的。"
    style_block = snapshot.instructions.split("# 客服语气配置", 1)[1].split(
        "\n\n# ",
        1,
    )[0]
    assert "你口吻拽拽的。" in style_block
    assert "正式但亲切的客服口吻。" not in style_block
    assert "本次浏览器测试 speaking_style 覆盖" not in snapshot.instructions
    assert captured["speaking_style"] == "你口吻拽拽的。"


def test_database_browser_prompt_section_overrides_replace_matching_sections():
    base_snapshot = PromptSnapshot(
        scene="collector-a:3",
        version="postgres",
        instructions=(
            "# 角色\n"
            "你是李经理。\n"
            "\n"
            "# 规则优先级\n"
            "1. 数据库原始规则优先级。\n"
            "\n"
            "# 高优先级运行红线\n"
            "1. 数据库原始运行红线。\n"
            "\n"
            "# 沟通规范\n"
            "1. 数据库原始沟通规范。"
        ),
        content_hash="base-hash",
        loaded_at_ms=1,
        metadata={
            "source": "postgres",
            "identityName": "collector-a",
            "personaId": "3",
            "debtId": "2049810626160668673",
        },
    )
    preparation = BusinessPromptPreparation(
        prompt_snapshot=base_snapshot,
        opening=None,
    )

    class Preparer:
        def prepare(self, context):
            return preparation

    store = BrowserPromptTestStore(business_prompt_preparer=Preparer())
    registration = store.register(
        {
            "call_id": "browser-section-replace",
            "mode": "database",
            "context": {
                "identityName": "collector-a",
                "debtId": "2049810626160668673",
            },
            "sections": {
                "rule_priority": ["浏览器规则优先级。"],
                "critical_runtime": ["浏览器运行红线。"],
                "communication_norms": ["浏览器沟通规范。"],
                "extra": ["浏览器额外补充。"],
            },
        }
    )

    instructions = registration.prompt_snapshot.instructions
    assert "# 规则优先级\n1. 浏览器规则优先级。" in instructions
    assert "# 高优先级运行红线\n1. 浏览器运行红线。" in instructions
    assert "# 沟通规范\n1. 浏览器沟通规范。" in instructions
    assert "数据库原始规则优先级" not in instructions
    assert "数据库原始运行红线" not in instructions
    assert "数据库原始沟通规范" not in instructions
    extra_block = instructions.split("# 浏览器测试补充规则", 1)[1]
    assert "浏览器额外补充。" in extra_block
    assert "浏览器规则优先级" not in extra_block
