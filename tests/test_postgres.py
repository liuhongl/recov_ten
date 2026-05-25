from __future__ import annotations

import asyncio
import json

from app.config import GatewayConfig, PostgresConfig
from app.flow_callback import FlowCallbackEvent
from app import postgres
from app.postgres import (
    BusinessPromptPreparation,
    PostgresCallDestinationStore,
    PostgresCallResultWriter,
    PostgresCallRecordStore,
    PostgresPromptStore,
    PostgresRuntime,
    ThreadsafeBusinessPromptPreparer,
    ThreadsafeCallRecordUpdater,
    build_call_record_transcript_json,
    fallback_prompt_snapshot,
)


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


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_fallback_prompt_snapshot_captures_prompt_identity():
    snapshot = fallback_prompt_snapshot("default", "be concise")

    assert snapshot.scene == "default"
    assert snapshot.version == "fallback"
    assert snapshot.instructions == "be concise"
    assert snapshot.content_hash
    assert snapshot.metadata == {"source": "fallback"}


def test_postgres_runtime_disabled_does_not_create_store_or_writer():
    asyncio.run(_assert_runtime_disabled_does_not_create_store_or_writer())


def test_postgres_runtime_missing_dsn_does_not_block_startup(monkeypatch):
    monkeypatch.delenv("TEST_POSTGRES_DSN", raising=False)

    asyncio.run(_assert_missing_dsn_does_not_block_startup())


def test_postgres_runtime_pool_failure_does_not_block_startup(monkeypatch):
    class BrokenAsyncpg:
        async def create_pool(self, **kwargs):
            raise OSError("postgres unavailable")

    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(postgres, "_load_asyncpg", lambda: BrokenAsyncpg())

    asyncio.run(_assert_pool_failure_does_not_block_startup())


def test_postgres_runtime_success_creates_prompt_store(monkeypatch):
    captured = {}

    class Pool:
        async def close(self):
            pass

    class FakeAsyncpg:
        async def create_pool(self, **kwargs):
            captured.update(kwargs)
            return Pool()

    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(postgres, "_load_asyncpg", lambda: FakeAsyncpg())

    asyncio.run(_assert_runtime_success_creates_prompt_store())

    assert captured["max_inactive_connection_lifetime"] == 0
    assert captured["server_settings"] == {
        "application_name": "recov_ten_gateway",
    }


def test_postgres_prompt_store_prepares_business_prompt_from_context():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                assert args == ("collector-a", "000000", "女")
                return None
            if "from call_identity_name" in query:
                assert args == ("collector-a",)
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                assert "speaking_style" in query
                assert "opening_template" in query
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

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "collector-a",
                "debtId": "2049810626160668673",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert isinstance(prep, BusinessPromptPreparation)
    assert prep.prompt_snapshot.scene == "collector-a:3"
    assert prep.prompt_snapshot.version == "postgres"
    assert "你是李经理" in prep.prompt_snapshot.instructions
    assert "先确认本人，再说明费用。" in prep.prompt_snapshot.instructions
    assert "业主称呼：金女士" in prep.prompt_snapshot.instructions
    assert "业主姓名：金阳" not in prep.prompt_snapshot.instructions
    assert "系统记录待处理金额：12.34" in prep.prompt_snapshot.instructions
    assert "# 金额与争议处理" in prep.prompt_snapshot.instructions
    assert "# 身份核实与隐私边界" in prep.prompt_snapshot.instructions
    assert "业主本人或该费用事项的授权处理人" in prep.prompt_snapshot.instructions
    assert "用户只说“好的”“嗯”“你说吧”“什么事”等" in prep.prompt_snapshot.instructions
    assert "不得主动披露具体姓名、地址、房号、待处理金额" in prep.prompt_snapshot.instructions
    assert "身份确认阶段只能使用业主称呼，不得说出完整姓名" in prep.prompt_snapshot.instructions
    assert "身份未确认时，下一句只能问：请问您是金女士本人，或方便处理这项物业费事项的授权处理人吗？" in prep.prompt_snapshot.instructions
    assert "这类身份核实句不得夹带地址、房号、待处理金额、欠费明细或费用原因" in prep.prompt_snapshot.instructions
    assert "只能说明“物业费事项”或“费用事项需要核实”" in prep.prompt_snapshot.instructions
    assert "# 身份确认后才可使用的信息" in prep.prompt_snapshot.instructions
    assert "以下信息即使系统已知，身份确认前也禁止说出" in prep.prompt_snapshot.instructions
    assert "用户主动询问欠款金额" in prep.prompt_snapshot.instructions
    assert "必须先确认对方是业主本人或授权处理人" in prep.prompt_snapshot.instructions
    assert "可以说明系统记录的待处理金额" in prep.prompt_snapshot.instructions
    assert "不要承诺减免、豁免利息" in prep.prompt_snapshot.instructions
    assert "不要确认用户已经还清" in prep.prompt_snapshot.instructions
    assert "安排物业工作人员或财务人员核对" in prep.prompt_snapshot.instructions
    assert prep.prompt_snapshot.metadata["source"] == "postgres"
    assert prep.prompt_snapshot.metadata["identityName"] == "collector-a"
    assert prep.prompt_snapshot.metadata["personaId"] == "3"
    assert prep.prompt_snapshot.metadata["debtId"] == "2049810626160668673"
    assert prep.prompt_snapshot.metadata["strategy_core"] == "先确认本人，再说明费用。"
    assert prep.prompt_snapshot.metadata["speaking_style"] == "正式但亲切的客服口吻。"
    assert "# 对话风格" in prep.prompt_snapshot.instructions
    assert "以已播放开场白为语气参照" in prep.prompt_snapshot.instructions
    assert "保持相同的身份、称呼方式、语气基调和沟通边界" in prep.prompt_snapshot.instructions
    assert "不要突然变得更强硬、更随意" in prep.prompt_snapshot.instructions
    assert "全程使用“您”" in prep.prompt_snapshot.instructions
    assert "不要说“你家”" in prep.prompt_snapshot.instructions
    assert "避免使用“尽快缴纳”“不影响物业服务”" in prep.prompt_snapshot.instructions
    assert "不得编造或猜测天气、新闻、时间" in prep.prompt_snapshot.instructions
    assert "不掌握该信息" in prep.prompt_snapshot.instructions
    assert "无租客信息时，不得主动假设存在租客" in prep.prompt_snapshot.instructions
    assert "不得建议联系租客" in prep.prompt_snapshot.instructions
    assert "# 规则优先级" in prep.prompt_snapshot.instructions
    assert "数据库催收策略、客户画像策略、客服语气配置与以下规则冲突时" in prep.prompt_snapshot.instructions
    assert "# 物业费场景约束" in prep.prompt_snapshot.instructions
    assert "没钱、资金困难、等工资" in prep.prompt_snapshot.instructions
    assert "优先询问方便哪天处理" in prep.prompt_snapshot.instructions
    assert "不主动追问工资日或收入情况" in prep.prompt_snapshot.instructions
    assert "延期、分期、部分付款" in prep.prompt_snapshot.instructions
    assert "不承诺批准、减免或结清" in prep.prompt_snapshot.instructions
    assert "物业服务问题" in prep.prompt_snapshot.instructions
    assert "这个问题我先记录并反馈项目核实" in prep.prompt_snapshot.instructions
    assert "不说“不交影响服务”" in prep.prompt_snapshot.instructions
    assert "不承诺具体维修安排、处理时间或满意结果" in prep.prompt_snapshot.instructions
    assert "不列举未提供的部门、师傅或维修方案" in prep.prompt_snapshot.instructions
    assert "拒付、情绪对抗、要求勿扰" in prep.prompt_snapshot.instructions
    assert "要求勿扰属于最高优先级" in prep.prompt_snapshot.instructions
    assert "不再追问原因、付款、回拨时间或费用安排" in prep.prompt_snapshot.instructions
    assert "账务、金额、收费标准争议" in prep.prompt_snapshot.instructions
    assert "支付安全、凭证、发票、已转账" in prep.prompt_snapshot.instructions
    assert "通过物业官方已公示渠道核实和办理" in prep.prompt_snapshot.instructions
    assert "不编造具体官方渠道名称、账号或缴费方式" in prep.prompt_snapshot.instructions
    assert "不列举未提供的前台、公众号、缴费机或账户" in prep.prompt_snapshot.instructions
    assert "不要再补充其他渠道名称" in prep.prompt_snapshot.instructions
    assert "忙碌、不方便" in prep.prompt_snapshot.instructions
    assert "不得冒充法院、司法机关、执法人员" in prep.prompt_snapshot.instructions
    assert "不能判断会不会起诉、影响征信或上门执行" in prep.prompt_snapshot.instructions
    assert "后续流程以物业方核实和正式通知为准" in prep.prompt_snapshot.instructions
    assert "法务/律师流程只能中性表述" in prep.prompt_snapshot.instructions
    assert "不得冒用律师、律师事务所、公检法或司法机关身份" in prep.prompt_snapshot.instructions
    assert "未明确委托关系时不得自称律师或受律师委托" in prep.prompt_snapshot.instructions
    assert "缴费意愿、费用处理安排" in prep.prompt_snapshot.instructions
    assert prep.opening.opening_text.startswith("您好，请问是金女士吗？我是李经理。")


def test_postgres_call_destination_store_resolves_debtor_phone_from_debt_id():
    class Conn:
        async def fetchrow(self, query, *args):
            assert "debtor_phone" in query
            assert "from debt_record" in query
            assert args == (2049810626160668673,)
            return {"debtor_phone": "15800967789"}

    store = PostgresCallDestinationStore(FakePool(Conn()))

    destination = asyncio.run(
        store.resolve_destination({"debtId": "2049810626160668673"})
    )

    assert destination == "15800967789"


def test_postgres_call_destination_store_returns_none_when_phone_missing():
    class Conn:
        async def fetchrow(self, query, *args):
            assert args == (2049810626160668673,)
            return {"debtor_phone": ""}

    store = PostgresCallDestinationStore(FakePool(Conn()))

    destination = asyncio.run(
        store.resolve_destination({"debtId": "2049810626160668673"})
    )

    assert destination is None


def test_postgres_prompt_store_uses_gender_matched_voice_config():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                assert "male_voice_gender" in query
                assert "female_voice_gender" in query
                assert "call_identity_name" in query
                assert "call_voice_library" in query
                assert args == ("项目员工", "000000", "女")
                return {
                    "gender_match": "1",
                    "selected_gender": "女",
                    "selected_voice_id": 1002,
                    "voice_name": "温和客服女声",
                    "base_voice_id": "zh_female_xiaohe_jupiter_bigtts",
                    "employee_name": "物业中心李晓莉",
                }
            if "from persona_call_strategy" in query:
                assert args == ("项目员工", 7)
                return {
                    "strategy_core": "先确认本人，再说明费用。",
                    "speaking_style": "正式但亲切的客服口吻。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
                assert args == (2056563388954320898,)
                return {
                    "debtor_name": "金阳",
                    "address": "测试小区一号楼",
                    "debt_amount": "12.34",
                    "debtor_gender": "女",
                    "debtor_age": 38,
                    "tenant_id": "000000",
                    "persona_id": 7,
                }
            if "from call_identity_name" in query:
                raise AssertionError("voice-matched employee should be used")
            raise AssertionError(query)

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "项目员工",
                "debtId": "2056563388954320898",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert prep.opening.voice == "温和客服女声"
    assert prep.opening.speaker == "zh_female_xiaohe_jupiter_bigtts"
    assert prep.prompt_snapshot.metadata["employee_name"] == "物业中心李晓莉"
    assert prep.prompt_snapshot.metadata["voice_name"] == "温和客服女声"
    assert prep.prompt_snapshot.metadata["speaker"] == "zh_female_xiaohe_jupiter_bigtts"
    assert prep.prompt_snapshot.metadata["voice_id"] == "1002"
    assert prep.prompt_snapshot.metadata["gender_match"] == "1"
    assert prep.prompt_snapshot.metadata["selected_gender"] == "女"


def test_postgres_prompt_store_uses_configured_voice_when_gender_match_disabled():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                assert args == ("项目员工", "000000", "女")
                return {
                    "gender_match": "0",
                    "selected_gender": "",
                    "selected_voice_id": 1002,
                    "voice_name": "温和客服女声",
                    "base_voice_id": "zh_female_xiaohe_jupiter_bigtts",
                    "employee_name": "物业中心李晓莉",
                }
            if "from persona_call_strategy" in query:
                assert args == ("项目员工", 7)
                return {
                    "strategy_core": "先确认本人，再说明费用。",
                    "speaking_style": "正式但亲切的客服口吻。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
                assert args == (2056563388954320898,)
                return {
                    "debtor_name": "金阳",
                    "address": "测试小区一号楼",
                    "debt_amount": "12.34",
                    "debtor_gender": "女",
                    "debtor_age": 38,
                    "tenant_id": "000000",
                    "persona_id": 7,
                }
            if "from call_identity_name" in query:
                raise AssertionError("configured voice employee should be used")
            raise AssertionError(query)

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "项目员工",
                "debtId": "2056563388954320898",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert prep.opening.voice == "温和客服女声"
    assert prep.opening.speaker == "zh_female_xiaohe_jupiter_bigtts"
    assert prep.prompt_snapshot.metadata["employee_name"] == "物业中心李晓莉"
    assert prep.prompt_snapshot.metadata["gender_match"] == "0"


def test_postgres_prompt_store_returns_none_when_business_context_missing():
    class Conn:
        async def fetchrow(self, query, *args):
            raise AssertionError("database should not be queried")

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {"identityName": "collector-a"},
            fallback_instructions="fallback",
        )
    )

    assert prep is None


def test_call_record_transcript_json_uses_existing_simple_turns_shape():
    transcript_json = build_call_record_transcript_json(
        {
            "opening": {"text": "您好，请问是金女士吗？"},
            "turns": [
                {"role": "assistant", "text": "您好，请问是金女士吗？"},
                {"role": "user", "text": "我是，你说吧。"},
                {"role": "assistant", "text": ""},
                {"role": "agent", "text": "不应保留非法角色。"},
            ],
        }
    )

    assert json.loads(transcript_json) == {
        "turns": [
            {"role": "assistant", "text": "您好，请问是金女士吗？"},
            {"role": "user", "text": "我是，你说吧。"},
        ]
    }


def test_postgres_call_record_store_marks_started_after_precheck():
    class Conn:
        def __init__(self):
            self.queries = []

        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, query, *args):
            self.queries.append(("fetchrow", query, args))
            assert "from public.call_record" in query
            assert args == (990000000000032001,)
            return {
                "id": 990000000000032001,
                "debt_id": 2049810626160668673,
                "status": "0",
                "transcript": None,
            }

        async def execute(self, query, *args):
            self.queries.append(("execute", query, args))
            assert "set status = '1'" in query.lower()
            assert "started_at = current_timestamp" in query
            assert "analysis_status" not in query
            assert args == (990000000000032001,)
            return "UPDATE 1"

    conn = Conn()
    store = PostgresCallRecordStore(FakePool(conn))

    updated = asyncio.run(
        store.mark_started(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            }
        )
    )

    assert updated is True
    assert [kind for kind, _, _ in conn.queries] == ["fetchrow", "execute"]


def test_postgres_call_record_store_does_not_update_when_debt_id_mismatches():
    class Conn:
        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, query, *args):
            return {
                "id": 990000000000032001,
                "debt_id": 111,
                "status": "0",
                "transcript": None,
            }

        async def execute(self, query, *args):
            raise AssertionError("mismatched debt_id must not update")

    store = PostgresCallRecordStore(FakePool(Conn()))

    updated = asyncio.run(
        store.mark_failed(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            }
        )
    )

    assert updated is False


def test_postgres_call_record_store_writes_completed_simple_transcript_only_from_running():
    class Conn:
        def __init__(self):
            self.executed_args = None

        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, query, *args):
            return {
                "id": 990000000000032001,
                "debt_id": 2049810626160668673,
                "status": "1",
                "transcript": None,
            }

        async def execute(self, query, *args):
            self.executed_args = args
            assert "set status = '4'" in query.lower()
            assert "transcript" in query
            assert "analysis_status" not in query
            assert "analysis_result" not in query
            return "UPDATE 1"

    conn = Conn()
    store = PostgresCallRecordStore(FakePool(conn))

    updated = asyncio.run(
        store.mark_transcript_completed(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            },
            '{"turns":[{"role":"assistant","text":"您好"}]}',
        )
    )

    assert updated is True
    assert conn.executed_args is not None
    assert conn.executed_args == (
        990000000000032001,
        '{"turns":[{"role":"assistant","text":"您好"}]}',
    )
    assert json.loads(conn.executed_args[1]) == {
        "turns": [{"role": "assistant", "text": "您好"}]
    }


def test_postgres_call_record_store_does_not_overwrite_terminal_status():
    class Conn:
        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, query, *args):
            return {
                "id": 990000000000032001,
                "debt_id": 2049810626160668673,
                "status": "4",
                "transcript": '{"turns":[]}',
            }

        async def execute(self, query, *args):
            raise AssertionError("terminal call_record must not be overwritten")

    store = PostgresCallRecordStore(FakePool(Conn()))

    updated = asyncio.run(
        store.mark_transcript_completed(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            },
            '{"turns":[{"role":"assistant","text":"新内容"}]}',
        )
    )

    assert updated is False


def test_postgres_call_result_writer_updates_call_record_with_simple_transcript():
    async def assert_writer():
        calls = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                calls.append((context, json.loads(transcript_json)))
                return True

        writer = PostgresCallResultWriter(Store())
        writer.start()
        try:
            assert writer.enqueue_nowait(
                {
                    "call_id": "internal-media-call",
                    "context": {
                        "callId": "990000000000032001",
                        "debtId": "2049810626160668673",
                    },
                    "turns": [
                        {"role": "assistant", "text": "您好"},
                        {"role": "user", "text": "我稍后处理"},
                        {"role": "agent", "text": "非法角色不入库"},
                    ],
                }
            )
            await asyncio.wait_for(writer.queue.join(), timeout=1.0)
        finally:
            await writer.stop()

        assert calls == [
            (
                {
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
                {
                    "turns": [
                        {"role": "assistant", "text": "您好"},
                        {"role": "user", "text": "我稍后处理"},
                    ]
                },
            )
        ]

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_emits_success_flow_callback_after_transcript_update():
    async def assert_writer():
        flow_events: list[FlowCallbackEvent] = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                return True

        class FakeFlowCallbackWriter:
            def publish(self, event):
                flow_events.append(event)
                return True

        writer = PostgresCallResultWriter(
            Store(),
            flow_callback_writer=FakeFlowCallbackWriter(),
        )
        writer.start()
        try:
            assert writer.enqueue_nowait(
                {
                    "call_id": "internal-media-call",
                    "context": {
                        "tenantId": "000000",
                        "taskId": "task-1",
                        "callId": "990000000000032001",
                        "debtId": "2049810626160668673",
                    },
                    "turns": [{"role": "assistant", "text": "您好"}],
                }
            )
            await asyncio.wait_for(writer.queue.join(), timeout=1.0)
        finally:
            await writer.stop()

        assert len(flow_events) == 1
        assert flow_events[0].status == "SUCCESS"
        assert flow_events[0].tenant_id == "000000"
        assert flow_events[0].task_id == "task-1"
        assert flow_events[0].business_id == "internal-media-call"
        assert flow_events[0].message == "外呼完成，转写已写入"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_does_not_emit_success_when_transcript_update_noops():
    async def assert_writer():
        flow_events: list[FlowCallbackEvent] = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                return False

        class FakeFlowCallbackWriter:
            def publish(self, event):
                flow_events.append(event)
                return True

        writer = PostgresCallResultWriter(
            Store(),
            flow_callback_writer=FakeFlowCallbackWriter(),
        )
        writer.start()
        try:
            assert writer.enqueue_nowait(
                {
                    "call_id": "internal-media-call",
                    "context": {
                        "tenantId": "000000",
                        "taskId": "task-1",
                        "callId": "990000000000032001",
                        "debtId": "2049810626160668673",
                    },
                    "turns": [{"role": "assistant", "text": "您好"}],
                }
            )
            await asyncio.wait_for(writer.queue.join(), timeout=1.0)
        finally:
            await writer.stop()

        assert flow_events == []

    asyncio.run(assert_writer())


def test_postgres_prompt_store_derives_persona_and_employee_from_debt_and_voice():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                assert args == ("项目员工", "000000", "女")
                return {
                    "gender_match": "1",
                    "selected_gender": "女",
                    "selected_voice_id": 1002,
                    "voice_name": "温和客服女声",
                    "base_voice_id": "zh_female_xiaohe_jupiter_bigtts",
                    "employee_name": "物业中心李晓莉",
                }
            if "from persona_call_strategy" in query:
                assert "speaking_style" in query
                assert "opening_template" in query
                assert args == ("项目员工", 7)
                return {
                    "strategy_core": "围绕物业费提醒。",
                    "speaking_style": "协调型、熟人式、耐心沟通的物业工作人员口吻。",
                    "opening_template": (
                        "您好，请问是{salutation}吗？我是{employee_name}。"
                        "这边想和您确认一下{address}的物业费事项，"
                        "系统显示目前还有{debt_amount}元待处理。"
                    ),
                }
            if "from debt_record" in query:
                assert args == (2056563388954320898,)
                return {
                    "debtor_name": "金阳",
                    "address": "测试小区一号楼",
                    "debt_amount": "12.34",
                    "debtor_gender": "女",
                    "debtor_age": 38,
                    "tenant_id": "000000",
                    "persona_id": 7,
                }
            if "from call_identity_name" in query:
                raise AssertionError("employeeName context should not be required")
            raise AssertionError(query)

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "项目员工",
                "debtId": "2056563388954320898",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert prep.prompt_snapshot.metadata["employee_name"] == "物业中心李晓莉"
    assert prep.prompt_snapshot.metadata["personaId"] == "7"
    assert (
        prep.prompt_snapshot.metadata["speaking_style"]
        == "协调型、熟人式、耐心沟通的物业工作人员口吻。"
    )
    assert prep.opening.speaking_style == "协调型、熟人式、耐心沟通的物业工作人员口吻。"
    assert "你是物业中心李晓莉" in prep.prompt_snapshot.instructions
    assert prep.opening.opening_text == (
        "您好，请问是金女士吗？我是物业中心李晓莉。"
        "这边有一项物业费事项需要和您本人核实一下，请问现在方便确认吗？"
    )


def test_threadsafe_business_prompt_preparer_runs_store_on_event_loop():
    async def assert_preparer():
        class Store:
            async def prepare_business_prompt(self, context, *, fallback_instructions):
                assert context == {"identityName": "collector-a"}
                assert fallback_instructions == "fallback"
                return "prepared"

        preparer = ThreadsafeBusinessPromptPreparer(
            asyncio.get_running_loop(),
            Store(),
            fallback_instructions="fallback",
            timeout_seconds=1.0,
        )
        result = await asyncio.to_thread(
            preparer.prepare,
            {"identityName": "collector-a"},
        )
        assert result == "prepared"

    asyncio.run(assert_preparer())


async def _assert_runtime_disabled_does_not_create_store_or_writer() -> None:
    runtime = PostgresRuntime(
        GatewayConfig(postgres=PostgresConfig(enabled=False)),
        fallback_instructions="fallback",
    )

    await runtime.start()
    await runtime.stop()

    assert runtime.pool is None
    assert runtime.prompt_store is None
    assert runtime.call_record_store is None
    assert runtime.call_record_updater is None
    assert runtime.call_result_writer is None


async def _assert_runtime_success_creates_prompt_store() -> None:
    runtime = PostgresRuntime(
        GatewayConfig(
            postgres=PostgresConfig(enabled=True, dsn_env="TEST_POSTGRES_DSN")
        ),
        fallback_instructions="fallback",
    )

    await runtime.start()

    assert runtime.pool is not None
    assert isinstance(runtime.prompt_store, PostgresPromptStore)
    assert isinstance(runtime.call_record_store, PostgresCallRecordStore)
    assert isinstance(runtime.call_record_updater, ThreadsafeCallRecordUpdater)
    assert isinstance(runtime.call_result_writer, PostgresCallResultWriter)

    await runtime.stop()
    assert runtime.pool is None
    assert runtime.prompt_store is None
    assert runtime.call_record_store is None
    assert runtime.call_record_updater is None
    assert runtime.call_result_writer is None


async def _assert_missing_dsn_does_not_block_startup() -> None:
    runtime = PostgresRuntime(
        GatewayConfig(
            postgres=PostgresConfig(enabled=True, dsn_env="TEST_POSTGRES_DSN")
        ),
        fallback_instructions="fallback",
    )

    await runtime.start()
    await runtime.stop()

    assert runtime.pool is None
    assert runtime.prompt_store is None
    assert runtime.call_record_store is None
    assert runtime.call_record_updater is None
    assert runtime.call_result_writer is None


async def _assert_pool_failure_does_not_block_startup() -> None:
    runtime = PostgresRuntime(
        GatewayConfig(
            postgres=PostgresConfig(enabled=True, dsn_env="TEST_POSTGRES_DSN")
        ),
        fallback_instructions="fallback",
    )

    await runtime.start()
    await runtime.stop()

    assert runtime.pool is None
    assert runtime.prompt_store is None
    assert runtime.call_record_store is None
    assert runtime.call_record_updater is None
    assert runtime.call_result_writer is None
