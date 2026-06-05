from __future__ import annotations

import asyncio
import json
import threading

import pytest

from app.config import CallRecordingConfig, GatewayConfig, PostgresConfig
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
from app.recording_upload import MinioRecordingStorage, OssConfig, SysOssRecord


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
    assert "12.34元" not in prep.prompt_snapshot.instructions
    assert "系统记录待处理金额：12.34" not in prep.prompt_snapshot.instructions
    assert "地址：测试小区一号楼" not in prep.prompt_snapshot.instructions
    assert "无论身份是否确认，均不得在通话中说出具体金额" in prep.prompt_snapshot.instructions
    assert "地址、房号和费用明细不写入本轮对话提示词" in prep.prompt_snapshot.instructions
    assert "# 金额与争议处理" in prep.prompt_snapshot.instructions
    assert "# 身份核实与隐私边界" in prep.prompt_snapshot.instructions
    assert "业主本人或该费用事项的授权处理人" in prep.prompt_snapshot.instructions
    assert "用户只说“方便”“可以”“好的”“嗯”“对”“是的”“在的”“你说吧”“什么事”等短句，不能视为已确认本人或授权处理人" in prep.prompt_snapshot.instructions
    assert "必须等用户明确说自己是业主本人、业主本人在接听、授权处理人，或明确表示自己可以处理该费用事项" in prep.prompt_snapshot.instructions
    assert "不得主动披露具体姓名、地址、房号、待处理金额" in prep.prompt_snapshot.instructions
    assert "身份确认阶段只能使用业主称呼，不得说出完整姓名" in prep.prompt_snapshot.instructions
    assert "身份未确认时，下一句只能问：请问您是金女士本人，或者是这项物业费事项的授权处理人吗？" in prep.prompt_snapshot.instructions
    assert "身份未确认时，下一句只能问：请问您是金女士本人，或方便处理这项物业费事项的授权处理人吗？" not in prep.prompt_snapshot.instructions
    assert "这类身份核实句不得夹带地址、房号、待处理金额、欠费明细或费用原因" in prep.prompt_snapshot.instructions
    assert "用户抱怨啰嗦、要求直接说、追问什么事但仍未确认身份时" in prep.prompt_snapshot.instructions
    assert "只能说明“物业费事项”或“费用事项需要核实”" in prep.prompt_snapshot.instructions
    assert "# 身份确认后的信息边界" in prep.prompt_snapshot.instructions
    assert "用户询问欠款金额、差多少钱或待处理金额时" in prep.prompt_snapshot.instructions
    assert "不得说出系统记录金额" in prep.prompt_snapshot.instructions
    assert "用户主动询问欠款金额" in prep.prompt_snapshot.instructions
    assert "必须先确认对方是业主本人或授权处理人" in prep.prompt_snapshot.instructions
    assert "确认后也不得在通话中说出具体金额" in prep.prompt_snapshot.instructions
    assert "不得复述用户提到的金额" in prep.prompt_snapshot.instructions
    assert "不得脱离本轮业务策略自行承诺减免、豁免利息" in prep.prompt_snapshot.instructions
    assert "以物业公司核实和办理为准" in prep.prompt_snapshot.instructions
    assert "不要确认用户已经还清" in prep.prompt_snapshot.instructions
    assert "安排物业工作人员或财务人员核对" in prep.prompt_snapshot.instructions
    assert prep.prompt_snapshot.metadata["source"] == "postgres"
    assert prep.prompt_snapshot.metadata["identityName"] == "collector-a"
    assert prep.prompt_snapshot.metadata["personaId"] == "3"
    assert prep.prompt_snapshot.metadata["debtId"] == "2049810626160668673"
    assert prep.prompt_snapshot.metadata["strategy_core"] == "先确认本人，再说明费用。"
    assert prep.prompt_snapshot.metadata["speaking_style"] == "正式但亲切的客服口吻。"
    assert "# 客服语气配置" in prep.prompt_snapshot.instructions
    assert "正式但亲切的客服口吻。" in prep.prompt_snapshot.instructions
    assert "# 对话风格" in prep.prompt_snapshot.instructions
    assert "数据库催收策略决定业务目标、推进方向和可表达的信息范围" in prep.prompt_snapshot.instructions
    assert "客服语气配置决定表达风格、正式程度和语气强弱" in prep.prompt_snapshot.instructions
    assert "不得让开场白反向覆盖数据库策略" in prep.prompt_snapshot.instructions
    assert "不得因策略阶段升级而忽略客服语气配置" in prep.prompt_snapshot.instructions
    assert "以已播放开场白为语气参照" not in prep.prompt_snapshot.instructions
    assert "保持相同的身份、称呼方式、语气基调和沟通边界" in prep.prompt_snapshot.instructions
    assert "不要突然变得更强硬、更随意" in prep.prompt_snapshot.instructions
    assert "全程使用“您”" in prep.prompt_snapshot.instructions
    assert "不要说“你家”" in prep.prompt_snapshot.instructions
    assert "避免使用“尽快缴纳”“不影响物业服务”" in prep.prompt_snapshot.instructions
    assert "不得编造或猜测天气、新闻、时间" in prep.prompt_snapshot.instructions
    assert "不掌握该信息" in prep.prompt_snapshot.instructions
    assert "无论用户是否主动提到租客" in prep.prompt_snapshot.instructions
    assert "不得索要租客联系方式" in prep.prompt_snapshot.instructions
    assert "不得建议联系租客" in prep.prompt_snapshot.instructions
    assert "# 高优先级运行红线" in prep.prompt_snapshot.instructions
    assert "不得再询问付款时间、缴费计划或租客联系方式" in prep.prompt_snapshot.instructions
    assert "不得主动询问发薪日" in prep.prompt_snapshot.instructions
    assert "不得列举前台、公告栏、单元门口" in prep.prompt_snapshot.instructions
    assert "不得说暂未涉及征信" in prep.prompt_snapshot.instructions
    assert "不得说为避免不必要的麻烦" in prep.prompt_snapshot.instructions
    assert "不得使用尽快处理" in prep.prompt_snapshot.instructions
    assert "用户已明确拒缴后" in prep.prompt_snapshot.instructions
    assert "回答发票、渠道、明细、征信、起诉等直接问题后必须继续收口" in prep.prompt_snapshot.instructions
    assert "不得再追问什么时候处理" in prep.prompt_snapshot.instructions
    assert "已确认身份后用户询问金额" in prep.prompt_snapshot.instructions
    assert "不得再次要求身份确认" in prep.prompt_snapshot.instructions
    assert "不得追问近期是否安排处理" in prep.prompt_snapshot.instructions
    assert "用户已确认身份后又问你是谁、你找我干什么" in prep.prompt_snapshot.instructions
    assert "只能回答身份与受托核实事项" in prep.prompt_snapshot.instructions
    assert "用户明确拒缴、说不想交、不交了或近期没有处理计划后" in prep.prompt_snapshot.instructions
    assert "不得附带后续处理入口、官方渠道办理、联系本人或处理计划" in prep.prompt_snapshot.instructions
    assert "固定回复“好的，我先记录您的反馈，就不再打扰您了。”" in prep.prompt_snapshot.instructions
    assert "用户表示已处理、已缴、已转账或已付款时，只能说明以物业系统或财务核对结果为准" in prep.prompt_snapshot.instructions
    assert "不得说我会反馈给物业核实、我反馈给物业、会反馈给物业" in prep.prompt_snapshot.instructions
    assert "用户表示先解决服务诉求再缴费、解决完再说、服务不好不交或类似条件拒缴时" in prep.prompt_snapshot.instructions
    assert "固定回复“好的，我先记录您的诉求，具体以物业核实为准。”" in prep.prompt_snapshot.instructions
    assert "用户问跟你有什么关系、为什么由你联系、你凭什么联系时" in prep.prompt_snapshot.instructions
    assert "只能说明“我是受物业公司委托，来核实物业费相关事项的。”" in prep.prompt_snapshot.instructions
    assert "法务或律师身份下，用户问为什么委托律师、怎么还委托律师、为什么找法务或怎么找上律师时" in prep.prompt_snapshot.instructions
    assert "不得说更正式的方式、及时处理、正式提醒或升级处理" in prep.prompt_snapshot.instructions
    assert "不得使用跟进、回访、后续联系等表述作为付款推进或记录理由" in prep.prompt_snapshot.instructions
    assert "不得说以便我们更好地跟进" in prep.prompt_snapshot.instructions
    assert "说明客服或物业身份时不得使用算是、应该是、差不多这类含糊措辞" in prep.prompt_snapshot.instructions
    assert "部分缴纳或费用减免" in prep.prompt_snapshot.instructions
    assert "不得脱离本轮业务策略自行承诺" in prep.prompt_snapshot.instructions
    assert "本轮业务策略未给出明确方案时只能记录意向" in prep.prompt_snapshot.instructions
    assert "不得说可以的、交多少都行" in prep.prompt_snapshot.instructions
    assert "用户提出电梯、维修、卫生、服务质量等投诉后" in prep.prompt_snapshot.instructions
    assert "不得在同一回复里继续催缴" in prep.prompt_snapshot.instructions
    assert "不得说还得麻烦您尽快处理" in prep.prompt_snapshot.instructions
    assert "我先记录您的诉求，具体以物业核实为准" in prep.prompt_snapshot.instructions
    assert "反馈给物业相关部门核实处理" not in prep.prompt_snapshot.instructions
    assert "反馈给项目核实处理" not in prep.prompt_snapshot.instructions
    assert "用户已明确拒缴或拒绝联系后，只允许收口一次" in prep.prompt_snapshot.instructions
    assert "不得反复附带后续若想处理" in prep.prompt_snapshot.instructions
    assert "记录反馈时只说记录您的反馈或诉求" in prep.prompt_snapshot.instructions
    assert "不得说记录您的态度" in prep.prompt_snapshot.instructions
    assert "用户否认本人后" in prep.prompt_snapshot.instructions
    assert "不得再次要求身份确认" in prep.prompt_snapshot.instructions
    assert "# 规则优先级" in prep.prompt_snapshot.instructions
    assert "数据库催收策略、客户画像策略、客服语气配置优先决定沟通方式" in prep.prompt_snapshot.instructions
    assert "不得突破身份核实、隐私保护、勿扰终止、支付安全、事实边界和法律红线" in prep.prompt_snapshot.instructions
    assert "# 物业费场景约束" in prep.prompt_snapshot.instructions
    assert "没钱、资金困难、等工资" in prep.prompt_snapshot.instructions
    assert "优先询问方便哪天处理" in prep.prompt_snapshot.instructions
    assert "不主动追问工资日或收入情况" in prep.prompt_snapshot.instructions
    assert "延期、分期、部分付款" in prep.prompt_snapshot.instructions
    assert "延期、分期、部分付款、费用减免" in prep.prompt_snapshot.instructions
    assert "不得脱离本轮业务策略自行新增批准、减免、结清或销账承诺" in prep.prompt_snapshot.instructions
    assert "引导客户通过物业官方已公示渠道联系物业公司核实" in prep.prompt_snapshot.instructions
    assert "不得自行承诺批准、减免、结清或销账" in prep.prompt_snapshot.instructions
    assert "部分缴纳未获本轮业务策略明确方案时" in prep.prompt_snapshot.instructions
    assert "数据库策略明确提供授权、条件和范围时，以数据库策略为准" not in prep.prompt_snapshot.instructions
    assert "可按授权说明是否可减免" not in prep.prompt_snapshot.instructions
    assert "物业服务问题" in prep.prompt_snapshot.instructions
    assert "我先记录您的诉求，具体以物业核实为准" in prep.prompt_snapshot.instructions
    assert "不说“不交影响服务”" in prep.prompt_snapshot.instructions
    assert "不承诺具体维修安排、处理时间或满意结果" in prep.prompt_snapshot.instructions
    assert "不承诺维修进度、修复时间、上门时间或回访时间" in prep.prompt_snapshot.instructions
    assert "不列举未提供的部门、师傅或维修方案" in prep.prompt_snapshot.instructions
    assert "拒付、情绪对抗、要求勿扰" in prep.prompt_snapshot.instructions
    assert "要求勿扰属于最高优先级" in prep.prompt_snapshot.instructions
    assert "不再追问原因、付款、回拨时间或费用安排" in prep.prompt_snapshot.instructions
    assert "后续只允许回答用户直接提出的必要问题" in prep.prompt_snapshot.instructions
    assert "账务、金额、收费标准争议" in prep.prompt_snapshot.instructions
    assert "支付安全、凭证、发票、已转账" in prep.prompt_snapshot.instructions
    assert "通过物业官方已公示渠道核实和办理" in prep.prompt_snapshot.instructions
    assert "不编造具体官方渠道名称、账号或缴费方式" in prep.prompt_snapshot.instructions
    assert "不得编造公众号、缴费入口、物业前台位置、发票开具规则或维修进度" in prep.prompt_snapshot.instructions
    assert "不列举未提供的前台、公众号、缴费机或账户" in prep.prompt_snapshot.instructions
    assert "不要再补充其他渠道名称" in prep.prompt_snapshot.instructions
    assert "忙碌、不方便" in prep.prompt_snapshot.instructions
    assert "业务不支持承诺回拨或约定回拨时间" in prep.prompt_snapshot.instructions
    assert "不得承诺财务、项目人员或客服会在某个具体时间回电" in prep.prompt_snapshot.instructions
    assert "询问方便回拨时间" not in prep.prompt_snapshot.instructions
    assert "安排物业工作人员回拨" not in prep.prompt_snapshot.instructions
    assert "已确认本人或授权处理人后，不得反复要求同一身份确认" in prep.prompt_snapshot.instructions
    assert "不得冒充法院、司法机关、执法人员" in prep.prompt_snapshot.instructions
    assert "不能判断会不会起诉、影响征信或上门执行" in prep.prompt_snapshot.instructions
    assert "后续流程以物业方核实和正式通知为准" in prep.prompt_snapshot.instructions
    assert "法务/律师流程只能中性表述" in prep.prompt_snapshot.instructions
    assert "不得冒用律师、律师事务所、公检法或司法机关身份" in prep.prompt_snapshot.instructions
    assert "未明确委托关系时不得自称律师或受律师委托" in prep.prompt_snapshot.instructions
    assert "未触发拒绝、勿扰或服务投诉时" in prep.prompt_snapshot.instructions
    assert prep.opening.opening_text.startswith("您好，请问是金女士吗？我是李经理。")


def test_postgres_prompt_store_sanitizes_strategy_conflicts_with_business_rules():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": (
                        "策略概述：企业客服接受投诉、承诺跟进；"
                        "另一方面说明缴费义务不因服务争议而自动解除。\n"
                        "正常保留：先记录客户服务投诉，再说明费用事项仍需核实。\n"
                        "明确告知投诉跟进周期，并约定“处理结果出来后我会第一时间联系您”。\n"
                        "将问题反馈给物业公司总部和区域管理中心，督促他们尽快检修。\n"
                        "设定期限：给出法律合规的行动窗口，请您在 X 日前完成缴纳。\n"
                        "如果业主提到房子出租，要求其提供租客联系方式并联系租客。\n"
                        "客户说没钱时，主动询问发薪日后哪天处理。\n"
                        "投诉事宜会由运营团队单独跟进，并移交到企业管理部门走投诉程序。"
                    ),
                    "speaking_style": (
                        "企业客服需要接受投诉、承诺跟进；"
                        "温和说明缴费义务不自动解除。"
                        "引导用户提供租客联系方式。"
                    ),
                    "opening_template": "",
                }
            if "from debt_record" in query:
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
                "identityName": "企业客服",
                "debtId": "2049810626160668673",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert "正常保留：先记录客户服务投诉" in prep.prompt_snapshot.instructions
    for forbidden in (
        "承诺跟进",
        "缴费义务",
        "跟进周期",
        "处理结果出来后",
        "第一时间联系",
        "物业公司总部",
        "区域管理中心",
        "督促他们尽快检修",
        "设定期限",
        "X 日前",
        "行动窗口",
        "租客联系方式",
        "联系租客",
        "主动询问发薪日",
        "运营团队",
        "单独跟进",
        "企业管理部门",
        "投诉程序",
    ):
        assert forbidden not in prep.prompt_snapshot.metadata["strategy_core"]
        assert forbidden not in prep.prompt_snapshot.metadata["speaking_style"]
        assert forbidden not in (prep.opening.speaking_style or "")
    for forbidden_instruction in (
        "要求其提供租客联系方式并联系租客",
        "主动询问发薪日后哪天处理",
        "给出法律合规的行动窗口",
        "请您在 X 日前完成缴纳",
        "投诉事宜会由运营团队单独跟进",
        "移交到企业管理部门走投诉程序",
    ):
        assert forbidden_instruction not in prep.prompt_snapshot.instructions


def test_postgres_prompt_store_preserves_database_fee_reduction_authorization():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "律师赵敏"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": (
                        "恶意对抗型律师策略：如果业主愿意一次性缴纳本金，"
                        "可以免除其滞纳金、违约金等附加费用。"
                    ),
                    "speaking_style": "中性、克制，按数据库授权说明费用减免条件。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
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

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "律师",
                "tenantId": "000000",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert "可以免除其滞纳金、违约金等附加费用" in prep.prompt_snapshot.instructions
    assert "可以免除其滞纳金、违约金等附加费用" in prep.prompt_snapshot.metadata["strategy_core"]
    assert "不得脱离本轮业务策略自行新增批准、减免、结清或销账承诺" in prep.prompt_snapshot.instructions
    assert "引导客户通过物业官方已公示渠道联系物业公司核实" in prep.prompt_snapshot.instructions
    assert "不得自行承诺批准、减免、结清或销账" in prep.prompt_snapshot.instructions
    assert "数据库策略明确提供授权、条件和范围时，以数据库策略为准" not in prep.prompt_snapshot.instructions
    assert "可按授权说明是否可减免" not in prep.prompt_snapshot.instructions
    assert "不承诺批准、减免或结清" not in prep.prompt_snapshot.instructions
    assert "不要承诺减免、豁免利息" not in prep.prompt_snapshot.instructions


def test_postgres_prompt_store_includes_historical_analysis_summaries():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": "先确认本人，再说明费用。",
                    "speaking_style": "正式但亲切的客服口吻。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
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
            assert "analysis_result" in query
            assert "from public.call_record" in query
            assert "id <> $3" in query
            assert args == (2049810626160668673, "tenant-a", 990000000000032001)
            return [
                {
                    "id": 1,
                    "analysis_result": json.dumps(
                        {
                            "summary": (
                                "用户表示测试小区一号楼12.34元物业费资金紧张，"
                                "承诺月底前处理。"
                            )
                        },
                        ensure_ascii=False,
                    ),
                },
                {
                    "id": 2,
                    "analysis_result": json.dumps(
                        {"summary": "用户希望先核对费用明细。"},
                        ensure_ascii=False,
                    ),
                },
                {"id": 3, "analysis_result": '{"summary": ""}'},
                {"id": 4, "analysis_result": "not-json"},
            ]

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "collector-a",
                "tenantId": "tenant-a",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    instructions = prep.prompt_snapshot.instructions
    assert "# 历史外呼摘要" in instructions
    assert "历史第1通：用户表示[地址已隐藏][金额已隐藏]物业费资金紧张，承诺月底前处理。" in instructions
    assert "用户表示测试小区一号楼12.34元物业费资金紧张" not in instructions
    assert "系统记录待处理金额：12.34元" not in instructions
    assert "无论身份是否确认，均不得在通话中说出具体金额" in instructions
    assert "测试小区一号楼" not in instructions
    assert "历史第2通：用户希望先核对费用明细。" in instructions
    assert "历史第3通" not in instructions
    assert "not-json" not in instructions
    assert "这些摘要按历史通话时间排列，不标注历史外呼身份" in instructions
    assert "不得根据历史第几通推断当时外呼身份" in instructions


def test_postgres_prompt_store_prevents_historical_summary_as_current_utterance():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": "根据客户最新表达推进沟通。",
                    "speaking_style": "自然礼貌。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
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
            return [
                {
                    "id": 1,
                    "analysis_result": json.dumps(
                        {
                            "summary": (
                                "用户曾反馈电梯常坏、卫生不佳，要求先处理服务问题。"
                            )
                        },
                        ensure_ascii=False,
                    ),
                },
            ]

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "企业法务",
                "tenantId": "000000",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    instructions = prep.prompt_snapshot.instructions
    assert "历史摘要不是用户本轮最新表达的内容来源" in instructions
    assert "不得说“您刚才提到/您提到电梯、卫生等问题”" in instructions
    assert "除非用户在本轮明确重新说出" in instructions


def test_postgres_prompt_store_sanitizes_legal_pressure_and_callback_style():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": (
                        "核心是厘清法律义务，强调缴费是合同义务；"
                        "后续物业可能会通过正式途径来处理相关事宜。"
                        "确认身份后做正式催告，说明账款已经进入法律跟进阶段。"
                        "记录显示客户曾于X日、X日两次承诺缴纳但未实际到账。"
                        "告知客户后续可能会面临诉讼。"
                        "正常保留：先确认身份，再说明费用事项需要核实。"
                    ),
                    "speaking_style": (
                        "语气正式，引用相关法律法规。"
                        "用正式催告口吻提醒客户尽快处理。"
                        "用户忙时说等您方便的时候我们再联系。"
                    ),
                    "opening_template": "",
                }
            if "from debt_record" in query:
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

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "企业法务",
                "tenantId": "000000",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    sanitized_sources = "\n".join(
        [
            prep.prompt_snapshot.metadata["strategy_core"],
            prep.prompt_snapshot.metadata["speaking_style"],
            prep.opening.speaking_style or "",
        ]
    )
    for forbidden in (
        "厘清法律义务",
        "合同义务",
        "正式途径来处理",
        "正式催告",
        "法律跟进阶段",
        "X日",
        "承诺缴纳",
        "未实际到账",
        "可能会面临诉讼",
        "相关法律法规",
        "尽快处理",
        "等您方便的时候我们再联系",
    ):
        assert forbidden not in sanitized_sources
    assert "正常保留：先确认身份" in prep.prompt_snapshot.instructions
    assert "不得使用正式催告、法律跟进阶段、可能面临诉讼等法律施压表达" in prep.prompt_snapshot.instructions
    assert "不得编造X日、X日承诺缴纳、未实际到账等系统未明确提供的事实" in prep.prompt_snapshot.instructions
    assert "用户提到起诉、法院、律师、征信或上门时，本轮回复只能中性收口" in prep.prompt_snapshot.instructions


def test_postgres_prompt_store_sanitizes_lawyer_legal_pressure_strategy():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "律师赵敏"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": (
                        "策略概述：律师联系投诉挂钩型业主时，需以法律视角厘清投诉权利与缴费义务的关系，"
                        "不进入服务质量的实质争论。"
                        "◆ 法律立场阐明：义务独立于投诉。"
                        "说明按照物业管理相关法律规定（如《民法典》合同编相关规定），"
                        "以服务质量不满为由拒缴物业费，在司法实践中通常难以获得支持。"
                        "告知业主正确的维权路径：向物业投诉→向主管部门投诉→提起诉讼，"
                        "而非以拒缴方式对抗。"
                        "若欠款进入诉讼，还需要承担诉讼费、律师费等费用。"
                        "正常保留：确认身份后，说明受物业委托核实物业费事项。"
                    ),
                    "speaking_style": (
                        "律师联系时以法律视角厘清投诉权利与缴费义务，"
                        "提醒用户服务问题不能作为拒缴物业费的理由。"
                    ),
                    "opening_template": "",
                }
            if "from debt_record" in query:
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

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "律师",
                "tenantId": "000000",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    sanitized_sources = "\n".join(
        [
            prep.prompt_snapshot.metadata["strategy_core"],
            prep.prompt_snapshot.metadata["speaking_style"],
            prep.opening.speaking_style or "",
        ]
    )
    for forbidden in (
        "法律视角",
        "缴费义务",
        "法律立场",
        "相关法律规定",
        "民法典",
        "司法实践",
        "维权路径",
        "主管部门",
        "提起诉讼",
        "协商解决方案",
        "给出行动期限",
        "欠款进入诉讼",
        "诉讼费",
        "律师费",
        "不能作为拒缴物业费的理由",
    ):
        assert forbidden not in sanitized_sources
    assert "正常保留：确认身份后" in prep.prompt_snapshot.instructions
    assert "法务或律师身份下也必须保持中性服务沟通" in prep.prompt_snapshot.instructions


def test_postgres_prompt_store_prevents_repeat_identity_and_payment_push_after_refusal():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "律师赵敏"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": "确认身份后说明费用事项。",
                    "speaking_style": "自然礼貌。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
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

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "律师",
                "tenantId": "000000",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    instructions = prep.prompt_snapshot.instructions
    assert "已确认身份后用户询问金额" in instructions
    assert "不得再次要求身份确认" in instructions
    assert "系统记录待处理金额：12.34元" not in instructions
    assert "可按系统记录待处理金额回答" not in instructions
    assert "不得说出系统记录金额" in instructions
    assert "不得复述用户提到的金额" in instructions
    assert "不得追问近期是否安排处理" in instructions
    assert "不得询问是否有缴费计划或处理计划" in instructions
    assert "用户已确认身份后又问你是谁、你找我干什么" in instructions
    assert "只能回答身份与受托核实事项" in instructions
    assert "不得使用跟进、回访、后续联系等表述作为付款推进或记录理由" in instructions
    assert "说明客服或物业身份时不得使用算是、应该是、差不多这类含糊措辞" in instructions
    assert "用户明确拒缴、说不想交、不交了或近期没有处理计划后" in instructions
    assert "固定回复“好的，我先记录您的反馈，就不再打扰您了。”" in instructions
    assert "请问您近期是否有安排处理这笔费用的计划" not in instructions


def test_postgres_prompt_store_uses_non_committal_service_feedback_wording():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_voice_config" in query:
                return None
            if "from call_identity_name" in query:
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                return {
                    "strategy_core": (
                        "用户投诉时反馈给物业相关部门核实处理，"
                        "详细记录并反馈给项目核实处理，督促他们核实处理。"
                    ),
                    "speaking_style": "强调反馈给物业相关部门核实处理。",
                    "opening_template": "",
                }
            if "from debt_record" in query:
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

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "企业客服",
                "tenantId": "000000",
                "debtId": "2049810626160668673",
                "callId": "990000000000032001",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    combined = "\n".join(
        [
            prep.prompt_snapshot.instructions,
            prep.prompt_snapshot.metadata["strategy_core"],
            prep.prompt_snapshot.metadata["speaking_style"],
            prep.opening.speaking_style or "",
        ]
    )
    assert "我先记录您的诉求，具体以物业核实为准" in combined
    assert "服务投诉同时伴随拒缴、不想交或没有处理计划时" in combined
    assert "不得追问其他处理想法" in combined
    for forbidden in (
        "反馈给物业相关部门核实处理",
        "反馈给项目核实处理",
        "督促他们核实处理",
        "以便他们核实处理",
    ):
        assert forbidden not in combined


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
                "recording_oss_id": None,
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


def test_postgres_call_record_store_reads_active_oss_config():
    class Conn:
        async def fetchrow(self, query, *args):
            assert "from public.sys_oss_config" in query.lower()
            assert "status = '0'" in query
            assert args == ()
            return {
                "endpoint": "minio.example:9000",
                "bucket_name": "recov",
                "access_key": "access",
                "secret_key": "secret",
                "prefix": "business",
                "is_https": "Y",
                "region": "",
                "domain": "cdn.example",
            }

    store = PostgresCallRecordStore(FakePool(Conn()))

    config = asyncio.run(store.get_active_oss_config())

    assert config == OssConfig(
        endpoint="minio.example:9000",
        bucket_name="recov",
        access_key="access",
        secret_key="secret",
        prefix="business",
        is_https=True,
        region="us-east-1",
        domain="cdn.example",
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("Y", True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("N", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
    ],
)
def test_postgres_call_record_store_parses_oss_https_flag(raw_value, expected):
    class Conn:
        async def fetchrow(self, query, *args):
            return {
                "endpoint": "minio.example:9000",
                "bucket_name": "recov",
                "access_key": "access",
                "secret_key": "secret",
                "prefix": "",
                "is_https": raw_value,
                "region": "cn-hz",
                "domain": "",
            }

    store = PostgresCallRecordStore(FakePool(Conn()))

    config = asyncio.run(store.get_active_oss_config())

    assert config is not None
    assert config.is_https is expected


def test_postgres_call_record_store_creates_sys_oss_and_marks_recording_uploaded():
    class Conn:
        def __init__(self):
            self.executed = []

        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, query, *args):
            assert "from public.call_record" in query.lower()
            assert args == (990000000000032001,)
            return {
                "id": 990000000000032001,
                "debt_id": 2049810626160668673,
                "status": "4",
                "transcript": '{"turns":[]}',
                "recording_oss_id": None,
            }

        async def execute(self, query, *args):
            self.executed.append((query, args))
            return "UPDATE 1" if "public.call_record" in query else "INSERT 0 1"

    conn = Conn()
    store = PostgresCallRecordStore(FakePool(conn))

    created = asyncio.run(
        store.create_sys_oss_record(
            SysOssRecord(
                oss_id=123456789,
                tenant_id="000000",
                file_name="recordings/000000/20260603/990000000000032001.wav",
                original_name="990000000000032001.wav",
                file_suffix=".wav",
                url="http://minio.example/recov/recordings/000000/20260603/990000000000032001.wav",
                ext1='{"fileSize":14,"contentType":"audio/wav"}',
                service="minio",
            )
        )
    )
    marked = asyncio.run(
        store.mark_recording_uploaded(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            },
            123456789,
        )
    )

    assert created is True
    assert marked is True
    assert len(conn.executed) == 2
    insert_query, insert_args = conn.executed[0]
    assert "insert into public.sys_oss" in insert_query.lower()
    assert insert_args[:8] == (
        123456789,
        "000000",
        "recordings/000000/20260603/990000000000032001.wav",
        "990000000000032001.wav",
        ".wav",
        "http://minio.example/recov/recordings/000000/20260603/990000000000032001.wav",
        '{"fileSize":14,"contentType":"audio/wav"}',
        "minio",
    )
    update_query, update_args = conn.executed[1]
    assert "recording_oss_id" in update_query
    assert update_args == (990000000000032001, 123456789)


def test_postgres_call_record_store_does_not_overwrite_recording_oss_id():
    class Conn:
        def __init__(self):
            self.executed = []

        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, query, *args):
            assert "from public.call_record" in query.lower()
            assert args == (990000000000032001,)
            return {
                "id": 990000000000032001,
                "debt_id": 2049810626160668673,
                "status": "4",
                "transcript": '{"turns":[]}',
                "recording_oss_id": 987654321,
            }

        async def execute(self, query, *args):
            self.executed.append((query, args))
            return "UPDATE 1"

    conn = Conn()
    store = PostgresCallRecordStore(FakePool(conn))

    marked = asyncio.run(
        store.mark_recording_uploaded(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            },
            123456789,
        )
    )

    assert marked is False
    assert conn.executed == []


def test_postgres_call_record_store_reads_existing_recording_oss_id():
    class Conn:
        async def fetchrow(self, query, *args):
            assert "from public.call_record" in query.lower()
            assert args == (990000000000032001,)
            return {
                "id": 990000000000032001,
                "debt_id": 2049810626160668673,
                "status": "4",
                "transcript": '{"turns":[]}',
                "recording_oss_id": 987654321,
            }

    store = PostgresCallRecordStore(FakePool(Conn()))

    oss_id = asyncio.run(
        store.get_existing_recording_oss_id(
            {
                "callId": "990000000000032001",
                "debtId": "2049810626160668673",
            }
        )
    )

    assert oss_id == 987654321


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


def test_call_record_transcript_json_preserves_handoff_speaker_metadata():
    transcript_json = build_call_record_transcript_json(
        {
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
            ]
        }
    )

    assert json.loads(transcript_json) == {
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
        ]
    }


def test_call_record_transcript_json_drops_unsupported_human_role():
    transcript_json = build_call_record_transcript_json(
        {
            "turns": [
                {
                    "role": "human",
                    "speaker_type": "human_agent",
                    "agent_id": "agent-1001",
                    "text": "您好，我是物业客服。",
                }
            ]
        }
    )

    assert json.loads(transcript_json) == {"turns": []}


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
        assert flow_events[0].business_id == "990000000000032001"
        assert flow_events[0].message == "外呼完成，转写已写入"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_keeps_success_when_recording_upload_fails():
    async def assert_writer():
        flow_events: list[FlowCallbackEvent] = []
        uploaded_payloads = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                return True

        class FailingRecordingUploader:
            async def upload_from_call_result(self, payload):
                uploaded_payloads.append(payload)
                raise RuntimeError("minio unavailable")

        class FakeFlowCallbackWriter:
            def publish(self, event):
                flow_events.append(event)
                return True

        writer = PostgresCallResultWriter(
            Store(),
            flow_callback_writer=FakeFlowCallbackWriter(),
            recording_uploader=FailingRecordingUploader(),
        )
        writer.start()
        try:
            assert writer.enqueue_nowait(
                {
                    "call_id": "internal-media-call",
                    "recording_path": (
                        "/var/lib/freeswitch/recordings/990000000000032001.wav"
                    ),
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

        assert len(uploaded_payloads) == 1
        assert len(flow_events) == 1
        assert flow_events[0].status == "SUCCESS"
        assert flow_events[0].message == "外呼完成，转写已写入"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_uploads_recording_before_success_callback():
    async def assert_writer():
        events = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                events.append("transcript")
                return True

        class RecordingUploader:
            async def upload_from_call_result(self, payload):
                events.append("recording_upload")
                return 123456789

        class FakeFlowCallbackWriter:
            def publish(self, event):
                events.append("success_callback")
                return True

        writer = PostgresCallResultWriter(
            Store(),
            flow_callback_writer=FakeFlowCallbackWriter(),
            recording_uploader=RecordingUploader(),
        )
        writer.start()
        try:
            assert writer.enqueue_nowait(
                {
                    "call_id": "internal-media-call",
                    "recording_path": (
                        "/var/lib/freeswitch/recordings/990000000000032001.wav"
                    ),
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

        assert events == ["transcript", "recording_upload", "success_callback"]

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_uses_explicit_business_id_for_success_callback():
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
                    "business_id": "biz-call-1",
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
        assert flow_events[0].business_id == "biz-call-1"
        assert flow_events[0].status == "SUCCESS"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_enqueue_is_thread_safe():
    async def assert_writer():
        loop_thread_id = threading.get_ident()
        put_thread_ids: list[int] = []
        calls = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                calls.append((context, json.loads(transcript_json)))
                return True

        writer = PostgresCallResultWriter(Store())
        writer.start()
        original_put_nowait = writer.queue.put_nowait

        def recording_put_nowait(payload):
            put_thread_ids.append(threading.get_ident())
            return original_put_nowait(payload)

        writer.queue.put_nowait = recording_put_nowait
        enqueue_results = []

        def enqueue_from_worker_thread():
            enqueue_results.append(
                writer.enqueue_nowait(
                    {
                        "call_id": "internal-media-call",
                        "context": {"callId": "990000000000032001"},
                        "turns": [{"role": "assistant", "text": "您好"}],
                    }
                )
            )

        await asyncio.to_thread(enqueue_from_worker_thread)
        try:
            await asyncio.wait_for(writer.queue.join(), timeout=1.0)
        finally:
            await writer.stop()

        assert enqueue_results == [True]
        assert put_thread_ids == [loop_thread_id]
        assert calls == [
            (
                {"callId": "990000000000032001"},
                {"turns": [{"role": "assistant", "text": "您好"}]},
            )
        ]

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_emits_failed_when_transcript_update_noops():
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

        assert len(flow_events) == 1
        assert flow_events[0].status == "FAILED"
        assert flow_events[0].tenant_id == "000000"
        assert flow_events[0].task_id == "task-1"
        assert flow_events[0].business_id == "990000000000032001"
        assert flow_events[0].message == "转写写入失败"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_marks_call_failed_for_failed_payload():
    async def assert_writer():
        store_events = []
        flow_events: list[FlowCallbackEvent] = []

        class Store:
            async def mark_failed(self, context):
                store_events.append(("failed", context))
                return True

            async def mark_transcript_completed(self, context, transcript_json):
                store_events.append(("transcript", context))
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
                    "status": "failed",
                    "failure_reason": "realtime_session_connect_failed",
                    "error": "Doubao S2S websocket handshake failed: HTTP 403",
                    "context": {
                        "tenantId": "000000",
                        "taskId": "task-1",
                        "callId": "990000000000032001",
                        "debtId": "2049810626160668673",
                    },
                    "turns": [],
                }
            )
            await asyncio.wait_for(writer.queue.join(), timeout=1.0)
        finally:
            await writer.stop()

        assert store_events == [
            (
                "failed",
                {
                    "tenantId": "000000",
                    "taskId": "task-1",
                    "callId": "990000000000032001",
                    "debtId": "2049810626160668673",
                },
            )
        ]
        assert len(flow_events) == 1
        assert flow_events[0].status == "FAILED"
        assert flow_events[0].message == "外呼失败：realtime_session_connect_failed"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_allows_local_outbound_test_without_call_record_update():
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
                        "taskId": "handoff-local-20260602043624-sfgk",
                        "callId": "handoff-local-20260602043624-sfgk",
                        "debtId": "2049810626160668673",
                        "scene": "local-outbound-test",
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
        assert flow_events[0].task_id == "handoff-local-20260602043624-sfgk"
        assert flow_events[0].business_id == "handoff-local-20260602043624-sfgk"
        assert flow_events[0].message == "外呼完成，本地测试未写入 call_record"

    asyncio.run(assert_writer())


def test_postgres_call_result_writer_emits_failed_when_transcript_update_raises():
    async def assert_writer():
        flow_events: list[FlowCallbackEvent] = []

        class Store:
            async def mark_transcript_completed(self, context, transcript_json):
                raise RuntimeError("database unavailable")

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
        assert flow_events[0].status == "FAILED"
        assert flow_events[0].business_id == "990000000000032001"
        assert flow_events[0].message == "转写写入失败"

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
    assert "协调型、熟人式、耐心沟通的物业工作人员口吻。" in prep.prompt_snapshot.instructions
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


def test_threadsafe_business_prompt_preparer_retries_transient_failure_once():
    async def assert_preparer():
        calls = 0

        class Store:
            async def prepare_business_prompt(self, context, *, fallback_instructions):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ConnectionError("stale pooled connection")
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
        assert calls == 2

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
            postgres=PostgresConfig(enabled=True, dsn_env="TEST_POSTGRES_DSN"),
            call_recording=CallRecordingConfig(upload_timeout_seconds=7.5),
        ),
        fallback_instructions="fallback",
    )

    await runtime.start()

    assert runtime.pool is not None
    assert isinstance(runtime.prompt_store, PostgresPromptStore)
    assert isinstance(runtime.call_record_store, PostgresCallRecordStore)
    assert isinstance(runtime.call_record_updater, ThreadsafeCallRecordUpdater)
    assert isinstance(runtime.call_result_writer, PostgresCallResultWriter)
    assert runtime.recording_uploader is not None
    assert isinstance(runtime.recording_uploader.storage, MinioRecordingStorage)
    assert runtime.recording_uploader.storage.timeout_seconds == 7.5

    await runtime.stop()
    assert runtime.pool is None
    assert runtime.prompt_store is None
    assert runtime.call_record_store is None
    assert runtime.call_record_updater is None
    assert runtime.call_result_writer is None
    assert runtime.recording_uploader is None


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
