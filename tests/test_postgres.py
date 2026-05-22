from __future__ import annotations

import asyncio

from app.config import GatewayConfig, PostgresConfig
from app import postgres
from app.postgres import (
    BusinessPromptPreparation,
    PostgresPromptStore,
    PostgresRuntime,
    ThreadsafeBusinessPromptPreparer,
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
    assert prep.opening.opening_text.startswith("您好，请问是金女士吗？我是李经理。")


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
    assert runtime.call_result_writer is None

    await runtime.stop()
    assert runtime.pool is None


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
    assert runtime.call_result_writer is None
