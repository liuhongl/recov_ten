from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .business_dialog_style import (
    BUSINESS_CRITICAL_RUNTIME_RULES,
    BUSINESS_DIALOG_SPEAKING_STYLE,
    numbered_business_amount_dispute_rules,
    numbered_business_communication_norms_rules,
    numbered_business_dialog_style_rules,
    numbered_business_fact_boundary_rules,
    numbered_business_property_fee_scene_rules,
    numbered_business_privacy_disclosure_rules,
    numbered_business_rule_priority_rules,
)
from .config import GatewayConfig
from .flow_callback import FlowCallbackWriterProtocol, build_flow_callback_event
from .opening import (
    OpeningGenerationFailed,
    OpeningRequest,
    build_business_opening_request,
)
from .recording_upload import (
    MinioRecordingStorage,
    OssConfig,
    RecordingUploadService,
    SysOssRecord,
)

LOGGER = logging.getLogger(__name__)
POSTGRES_APPLICATION_NAME = "recov_ten_gateway"
LOCAL_OUTBOUND_TEST_SCENE = "local-outbound-test"

IDENTITY_NAME_SQL = """
select name
from call_identity_name
where identity_name = $1
order by random()
limit 1
"""

STRATEGY_SQL = """
select strategy_core, speaking_style, opening_template
from persona_call_strategy
where identity_name = $1 and persona_id = $2
limit 1
"""

DEBT_RECORD_SQL = """
select debtor_name, address, debt_amount, debtor_gender, debtor_age, tenant_id, persona_id
from debt_record
where id = $1
limit 1
"""

DEBT_RECORD_PHONE_SQL = """
select debtor_phone
from debt_record
where id = $1
limit 1
"""

VOICE_EMPLOYEE_SQL = """
select
  vc.gender_match,
  vc.voice_id as config_voice_id,
  vc.male_voice_gender,
  vc.female_voice_gender,
  case
    when vc.gender_match = '1' and $3 = '男' then vc.male_voice_gender
    when vc.gender_match = '1' and $3 = '女' then vc.female_voice_gender
    else ''
  end as selected_gender,
  cin.name as employee_name,
  cin.voice_id as selected_voice_id,
  lib.voice_name,
  lib.base_voice_id
from call_voice_config vc
join call_identity_name cin
  on cin.identity_name = vc.identity_name
 and cin.tenant_id = vc.tenant_id
join call_voice_library lib
  on lib.id = cin.voice_id
where vc.identity_name = $1
  and vc.tenant_id = $2
  and (
    (coalesce(vc.gender_match, '0') <> '1' and cin.voice_id = vc.voice_id)
    or (
      vc.gender_match = '1'
      and lib.gender = case
        when $3 = '男' then vc.male_voice_gender
        when $3 = '女' then vc.female_voice_gender
        else null
      end
    )
  )
order by random()
limit 1
"""

CALL_RECORD_SELECT_SQL = """
select
  id,
  debt_id,
  status,
  transcript,
  recording_oss_id
from public.call_record
where id = $1
limit 1
"""

CALL_RECORD_START_SQL = """
update public.call_record
set status = '1',
    started_at = current_timestamp,
    update_time = current_timestamp
where id = $1
  and status in ('0', '1')
"""

CALL_RECORD_FAILED_SQL = """
update public.call_record
set status = '2',
    finished_at = current_timestamp,
    update_time = current_timestamp
where id = $1
  and status in ('0', '1')
"""

CALL_RECORD_NO_ANSWER_SQL = """
update public.call_record
set status = '3',
    finished_at = current_timestamp,
    update_time = current_timestamp
where id = $1
  and status in ('0', '1')
"""

CALL_RECORD_TRANSCRIPT_COMPLETED_SQL = """
update public.call_record
set status = '4',
    finished_at = current_timestamp,
    transcript = $2,
    update_time = current_timestamp
where id = $1
  and status = '1'
"""

CALL_RECORDING_OSS_UPDATE_SQL = """
update public.call_record
set recording_oss_id = $2,
    update_time = current_timestamp
where id = $1
"""

SYS_OSS_CONFIG_SELECT_SQL = """
select endpoint, bucket_name, access_key, secret_key, prefix, is_https, region, domain
from public.sys_oss_config
where status = '0'
limit 1
"""

SYS_OSS_INSERT_SQL = """
insert into public.sys_oss (
  oss_id,
  tenant_id,
  file_name,
  original_name,
  file_suffix,
  url,
  ext1,
  service,
  create_time,
  update_time
) values (
  $1, $2, $3, $4, $5, $6, $7, $8, current_timestamp, current_timestamp
)
"""

CALL_RECORD_HISTORICAL_SUMMARIES_SQL = """
select
  id,
  finished_at,
  analysis_result
from public.call_record
where debt_id = $1
  and ($2::text is null or tenant_id = $2)
  and id <> $3
  and status = '4'
  and analysis_status = '2'
  and analysis_result is not null
  and btrim(analysis_result) <> ''
order by finished_at asc nulls last, id asc
limit 8
"""

CALL_RECORD_TERMINAL_STATUSES = {"2", "3", "4"}
HISTORICAL_SUMMARY_MAX_CHARS = 200
HISTORICAL_SUMMARY_BLOCK_MAX_CHARS = 1500
PROMPT_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*元")
CONFLICTING_STRATEGY_MARKERS = (
    "承诺跟进",
    "投诉跟进承诺",
    "跟进周期",
    "第一时间联系",
    "主动联系",
    "主动回电",
    "回拨",
    "回电",
    "等您方便",
    "再联系",
    "稍后联系",
    "下次联系",
    "处理结果出来后",
    "处理结果出来",
    "物业公司总部",
    "区域管理中心",
    "物业相关部门",
    "相关部门",
    "反馈给物业",
    "反馈给项目",
    "核实处理",
    "督促他们核实处理",
    "以便他们核实处理",
    "详细记录并反馈",
    "督促",
    "尽快检修",
    "维修进度",
    "修复时间",
    "跟进节点",
    "绑定到投诉解决时间",
    "设定期限",
    "行动窗口",
    "行动期限",
    "X 日前",
    "日前完成缴纳",
    "本周内完成缴纳",
    "本周五前",
    "本月底前",
    "缴款期限",
    "给出缴款期限",
    "租客联系方式",
    "承租人联系方式",
    "联系租客",
    "找租客",
    "和租客沟通",
    "提供租客",
    "提供承租人",
    "主动询问发薪",
    "发薪日",
    "工资日",
    "收入情况",
    "物业前台",
    "前台联系",
    "公告栏",
    "单元门口",
    "运营团队",
    "单独跟进",
    "企业管理部门",
    "管理部门",
    "投诉程序",
    "协商解决方案",
    "行使路径",
    "移交到",
    "法律视角",
    "法律立场",
    "缴费义务",
    "义务独立于投诉",
    "相关法律规定",
    "相关规定",
    "民法典",
    "司法实践",
    "获得支持",
    "维权路径",
    "主管部门",
    "提起诉讼",
    "拒缴方式",
    "拒缴物业费",
    "不能作为拒缴物业费的理由",
    "服务质量问题不能作为",
    "先缴清",
    "优先跟进",
    "欠款进入诉讼",
    "诉讼费",
    "律师费",
    "混为一谈",
    "暂未涉及征信",
    "不会影响征信",
    "会影响征信",
    "法律义务",
    "合同义务",
    "相关法律法规",
    "正式途径来处理",
    "通过正式途径",
    "正式催告",
    "法律跟进",
    "法律跟进阶段",
    "可能面临诉讼",
    "可能会面临诉讼",
    "面临诉讼",
    "承诺缴纳",
    "承诺缴费",
    "未实际到账",
    "X日",
    "X 日",
    "避免不必要的麻烦",
    "尽快处理",
    "尽快缴纳",
    "还得麻烦",
    "还得麻烦您",
    "建议您还是处理",
    "交多少都行",
    "根据自己的情况安排",
    "根据自己情况安排",
    "直接销掉",
    "销掉记录",
    "后续若想处理",
    "后续如果您有缴费意愿",
    "若之后您想处理",
    "联系我",
    "再联系我",
    "协助您同步",
    "协助同步",
    "他们会留意",
    "保证后续不会再打扰",
    "记录您的态度",
    "您的态度反馈",
    "把您的态度反馈",
    "将您的态度反馈",
    "情况反馈给物业",
)
ALLOWED_NEGATED_MARKERS = (
    "不主动",
    "不得",
    "不要",
    "不承诺",
    "不得承诺",
    "不支持承诺",
    "不能承诺",
    "禁止承诺",
    "不再",
)


@dataclass(frozen=True)
class PromptSnapshot:
    scene: str
    version: str
    instructions: str
    content_hash: str
    loaded_at_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "version": self.version,
            "content_hash": self.content_hash,
            "instructions": self.instructions,
            "loaded_at_ms": self.loaded_at_ms,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class BusinessPromptPreparation:
    prompt_snapshot: PromptSnapshot
    opening: OpeningRequest


@dataclass(frozen=True)
class VoiceSelection:
    voice_id: str
    voice_name: str
    speaker: str
    gender_match: str
    employee_name: str
    selected_gender: str


@dataclass(frozen=True)
class BusinessCallRecordRef:
    call_id: int
    debt_id: int


@dataclass(frozen=True)
class HistoricalCallSummary:
    call_id: str
    summary: str


class AsyncBusinessPromptStoreProtocol(Protocol):
    async def prepare_business_prompt(
        self,
        context: Mapping[str, Any],
        *,
        fallback_instructions: str,
    ) -> BusinessPromptPreparation | None: ...


class RecordingUploaderProtocol(Protocol):
    async def upload_from_call_result(self, payload: Mapping[str, Any]) -> int | None: ...


class PostgresPromptStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def get_prompt_snapshot(
        self,
        scene: str | None = None,
        *,
        fallback_instructions: str | None = None,
    ) -> PromptSnapshot:
        return fallback_prompt_snapshot(scene or "default", fallback_instructions or "")

    async def prepare_business_prompt(
        self,
        context: Mapping[str, Any],
        *,
        fallback_instructions: str,
    ) -> BusinessPromptPreparation | None:
        params = _business_prompt_params(context)
        if params is None:
            return None

        identity_name, debt_id = params
        identity_row = None
        strategy_row = None
        debt_row = None
        voice_row = None
        async with self.pool.acquire() as conn:
            debt_row = await conn.fetchrow(DEBT_RECORD_SQL, debt_id)
            if debt_row is not None:
                persona_id = _context_int(_row_value(debt_row, "persona_id"))
                if persona_id is not None:
                    strategy_row = await conn.fetchrow(
                        STRATEGY_SQL,
                        identity_name,
                        persona_id,
                    )
                voice_row = await conn.fetchrow(
                    VOICE_EMPLOYEE_SQL,
                    identity_name,
                    _row_value(debt_row, "tenant_id"),
                    _prompt_text(_row_value(debt_row, "debtor_gender")),
                )
            if voice_row is None:
                identity_row = await conn.fetchrow(IDENTITY_NAME_SQL, identity_name)

        voice_selection = _voice_selection_from_row(voice_row)
        persona_id = (
            None if debt_row is None else _context_int(_row_value(debt_row, "persona_id"))
        )
        if (
            debt_row is None
            or strategy_row is None
            or (voice_selection is None and identity_row is None)
            or persona_id is None
        ):
            LOGGER.warning(
                "business_prompt_lookup_missing identityName=%s personaId=%s "
                "debtId=%s has_identity=%s has_strategy=%s has_debt=%s",
                identity_name,
                persona_id,
                debt_id,
                voice_selection is not None or identity_row is not None,
                strategy_row is not None,
                debt_row is not None,
            )
            return None

        async with self.pool.acquire() as conn:
            historical_summaries = await _load_historical_call_summaries(
                conn,
                context,
            )
        employee_name = (
            voice_selection.employee_name
            if voice_selection is not None
            else _row_value(identity_row, "name")
        )
        strategy = _sanitize_business_strategy_text(
            _row_value(strategy_row, "strategy_core")
        )
        speaking_style = _sanitize_business_strategy_text(
            _row_value(strategy_row, "speaking_style"),
            append_note=False,
        )
        opening_template = _row_value(strategy_row, "opening_template")
        debtor_name = _row_value(debt_row, "debtor_name")
        address = _row_value(debt_row, "address")
        debt_amount = _row_value(debt_row, "debt_amount")
        debtor_gender = _row_value(debt_row, "debtor_gender")
        debtor_age = _row_value(debt_row, "debtor_age")
        try:
            opening = build_business_opening_request(
                employee_name=employee_name,
                debtor_name=debtor_name,
                debtor_gender=debtor_gender,
                debt_amount=debt_amount,
                address=address,
                speaking_style=speaking_style,
                opening_template=opening_template,
                voice=(
                    "female" if voice_selection is None else voice_selection.voice_name
                ),
                speaker=None if voice_selection is None else voice_selection.speaker,
            )
        except OpeningGenerationFailed:
            LOGGER.warning(
                "business_opening_render_failed identityName=%s personaId=%s "
                "debtId=%s",
                identity_name,
                persona_id,
                debt_id,
                exc_info=True,
            )
            return None

        instructions = _render_business_prompt(
            employee_name=employee_name,
            strategy=strategy,
            speaking_style=speaking_style,
            debtor_name=debtor_name,
            debtor_gender=debtor_gender,
            debtor_age=debtor_age,
            debt_amount=debt_amount,
            address=address,
            history_summary_block=_render_history_summary_block(
                historical_summaries,
                debt_amount=debt_amount,
                address=address,
            ),
        )
        metadata = {
            "source": "postgres",
            "identityName": identity_name,
            "personaId": str(persona_id),
            "debtId": str(debt_id),
            "employee_name": _prompt_text(employee_name),
            "strategy_core": _prompt_text(strategy),
            "speaking_style": _prompt_text(speaking_style),
            "opening_text_hash": opening.opening_text_hash,
        }
        if voice_selection is not None:
            metadata.update(
                {
                    "voice_source": "call_voice_config",
                    "voice_id": voice_selection.voice_id,
                    "voice_name": voice_selection.voice_name,
                    "speaker": voice_selection.speaker,
                    "gender_match": voice_selection.gender_match,
                    "selected_gender": voice_selection.selected_gender,
                }
            )

        return BusinessPromptPreparation(
            prompt_snapshot=PromptSnapshot(
                scene=f"{identity_name}:{persona_id}",
                version="postgres",
                instructions=instructions,
                content_hash=_hash_text(instructions),
                loaded_at_ms=_now_ms(),
                metadata=metadata,
            ),
            opening=opening,
        )


class PostgresCallRecordStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def mark_started(self, context: Mapping[str, Any]) -> bool:
        return await self._update_status(
            context,
            sql=CALL_RECORD_START_SQL,
            allowed_statuses={"0", "1"},
        )

    async def mark_failed(self, context: Mapping[str, Any]) -> bool:
        return await self._update_status(
            context,
            sql=CALL_RECORD_FAILED_SQL,
            allowed_statuses={"0", "1"},
        )

    async def mark_no_answer(self, context: Mapping[str, Any]) -> bool:
        return await self._update_status(
            context,
            sql=CALL_RECORD_NO_ANSWER_SQL,
            allowed_statuses={"0", "1"},
        )

    async def mark_transcript_completed(
        self,
        context: Mapping[str, Any],
        transcript_json: str,
    ) -> bool:
        params = _business_call_record_params(context)
        if params is None:
            LOGGER.warning("call_record_update_skipped_missing_context")
            return False
        try:
            json.loads(transcript_json)
        except json.JSONDecodeError:
            LOGGER.warning(
                "call_record_transcript_update_skipped_invalid_json callId=%s",
                params.call_id,
            )
            return False

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(CALL_RECORD_SELECT_SQL, params.call_id)
                if not _call_record_precheck_passed(
                    row,
                    params,
                    allowed_statuses={"1"},
                ):
                    return False
                result = await conn.execute(
                    CALL_RECORD_TRANSCRIPT_COMPLETED_SQL,
                    params.call_id,
                    transcript_json,
                )
                return _execute_updated_row(result)

    async def get_active_oss_config(self) -> OssConfig | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(SYS_OSS_CONFIG_SELECT_SQL)
        if row is None:
            return None
        return _oss_config_from_row(row)

    async def create_sys_oss_record(self, record: SysOssRecord) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                SYS_OSS_INSERT_SQL,
                record.oss_id,
                record.tenant_id,
                record.file_name,
                record.original_name,
                record.file_suffix,
                record.url,
                record.ext1,
                record.service,
            )
        return _execute_inserted_row(result)

    async def get_existing_recording_oss_id(
        self,
        context: Mapping[str, Any],
    ) -> int | None:
        params = _business_call_record_params(context)
        if params is None:
            LOGGER.warning("call_record_recording_lookup_skipped_missing_context")
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(CALL_RECORD_SELECT_SQL, params.call_id)
        if row is None:
            LOGGER.warning(
                "call_record_recording_lookup_skipped_missing callId=%s",
                params.call_id,
            )
            return None
        debt_id = _context_int(_row_value(row, "debt_id"))
        if debt_id != params.debt_id:
            LOGGER.warning(
                "call_record_recording_lookup_skipped_debt_mismatch "
                "callId=%s expectedDebtId=%s",
                params.call_id,
                params.debt_id,
            )
            return None
        return _context_int(_row_value(row, "recording_oss_id"))

    async def mark_recording_uploaded(
        self,
        context: Mapping[str, Any],
        oss_id: int,
    ) -> bool:
        params = _business_call_record_params(context)
        if params is None:
            LOGGER.warning("call_record_recording_update_skipped_missing_context")
            return False

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(CALL_RECORD_SELECT_SQL, params.call_id)
                if row is None:
                    LOGGER.warning(
                        "call_record_recording_update_skipped_missing callId=%s",
                        params.call_id,
                    )
                    return False
                debt_id = _context_int(_row_value(row, "debt_id"))
                if debt_id != params.debt_id:
                    LOGGER.warning(
                        "call_record_recording_update_skipped_debt_mismatch "
                        "callId=%s expectedDebtId=%s",
                        params.call_id,
                        params.debt_id,
                    )
                    return False
                existing_oss_id = _context_int(_row_value(row, "recording_oss_id"))
                if existing_oss_id is not None:
                    LOGGER.warning(
                        "call_record_recording_update_skipped_already_uploaded "
                        "callId=%s existingOssId=%s",
                        params.call_id,
                        existing_oss_id,
                    )
                    return False
                result = await conn.execute(
                    CALL_RECORDING_OSS_UPDATE_SQL,
                    params.call_id,
                    oss_id,
                )
                return _execute_updated_row(result)

    async def _update_status(
        self,
        context: Mapping[str, Any],
        *,
        sql: str,
        allowed_statuses: set[str],
    ) -> bool:
        params = _business_call_record_params(context)
        if params is None:
            LOGGER.warning("call_record_update_skipped_missing_context")
            return False

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(CALL_RECORD_SELECT_SQL, params.call_id)
                if not _call_record_precheck_passed(
                    row,
                    params,
                    allowed_statuses=allowed_statuses,
                ):
                    return False
                result = await conn.execute(sql, params.call_id)
                return _execute_updated_row(result)


class PostgresCallDestinationStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def resolve_destination(self, context: Mapping[str, Any]) -> str | None:
        debt_id = _context_int(context.get("debtId"))
        if debt_id is None:
            LOGGER.warning("call_destination_lookup_skipped_missing_debt_id")
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(DEBT_RECORD_PHONE_SQL, debt_id)
        if row is None:
            LOGGER.warning("call_destination_lookup_missing_debt debtId=%s", debt_id)
            return None
        phone = _context_text(_row_value(row, "debtor_phone"))
        if phone is None:
            LOGGER.warning("call_destination_lookup_missing_phone debtId=%s", debt_id)
            return None
        return phone


class ThreadsafeCallDestinationResolver:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        store: PostgresCallDestinationStore,
        *,
        timeout_seconds: float,
    ) -> None:
        self.loop = loop
        self.store = store
        self.timeout_seconds = timeout_seconds

    def resolve(self, context: Mapping[str, Any]) -> str | None:
        future = asyncio.run_coroutine_threadsafe(
            self.store.resolve_destination(context),
            self.loop,
        )
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as err:
            future.cancel()
            raise RuntimeError("call_destination_lookup_timeout") from err


class ThreadsafeCallRecordUpdater:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        store: PostgresCallRecordStore,
        *,
        timeout_seconds: float,
    ) -> None:
        self.loop = loop
        self.store = store
        self.timeout_seconds = timeout_seconds

    def mark_started(self, context: Mapping[str, Any]) -> bool:
        return self._run(self.store.mark_started(context))

    def mark_failed(self, context: Mapping[str, Any]) -> bool:
        return self._run(self.store.mark_failed(context))

    def mark_no_answer(self, context: Mapping[str, Any]) -> bool:
        return self._run(self.store.mark_no_answer(context))

    def _run(self, coro) -> bool:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return bool(future.result(timeout=self.timeout_seconds))
        except FutureTimeoutError:
            future.cancel()
            LOGGER.warning("call_record_update_timeout", exc_info=True)
            return False
        except Exception:
            LOGGER.warning("call_record_update_failed", exc_info=True)
            return False


class PostgresCallResultWriter:
    def __init__(
        self,
        store: PostgresCallRecordStore,
        *,
        max_queue_size: int = 100,
        flow_callback_writer: FlowCallbackWriterProtocol | None = None,
        recording_uploader: RecordingUploaderProtocol | None = None,
    ) -> None:
        self.store = store
        self.flow_callback_writer = flow_callback_writer
        self.recording_uploader = recording_uploader
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._task is None:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(
                self._run(),
                name="postgres-call-result-writer",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._loop = None

    def enqueue_nowait(self, payload: dict) -> bool:
        loop = self._loop
        if loop is None or loop.is_closed():
            return self._put_nowait(payload)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            return self._put_nowait(payload)

        result: Future[bool] = Future()

        def put_on_loop() -> None:
            if result.done():
                return
            try:
                result.set_result(self._put_nowait(payload))
            except Exception as err:
                result.set_exception(err)

        try:
            loop.call_soon_threadsafe(put_on_loop)
            return result.result(timeout=1.0)
        except FutureTimeoutError:
            LOGGER.warning("call_result_writer_enqueue_timeout")
            return False
        except RuntimeError:
            return False

    def _put_nowait(self, payload: dict) -> bool:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            return False
        return True

    async def _run(self) -> None:
        while True:
            payload = await self.queue.get()
            context = payload.get("context")
            if not isinstance(context, Mapping):
                context = {}
            try:
                if payload.get("status") == "failed":
                    updated = await self.store.mark_failed(context)
                    if not updated:
                        LOGGER.warning(
                            "call_record_failed_update_noop call_id=%s",
                            payload.get("call_id"),
                        )
                    self._publish_failure_callback(
                        payload,
                        context,
                        message=_call_result_failure_message(payload),
                    )
                    continue
                transcript_json = build_call_record_transcript_json(payload)
                updated = await self.store.mark_transcript_completed(
                    context,
                    transcript_json,
                )
                if not updated:
                    LOGGER.warning(
                        "call_record_transcript_update_noop call_id=%s",
                        payload.get("call_id"),
                    )
                    if _is_local_outbound_test_context(context):
                        self._publish_success_callback(
                            payload,
                            context,
                            message="外呼完成，本地测试未写入 call_record",
                        )
                    else:
                        self._publish_failure_callback(payload, context)
                else:
                    await self._upload_recording(payload)
                    self._publish_success_callback(payload, context)
            except Exception:
                LOGGER.warning(
                    "call_record_transcript_update_failed call_id=%s",
                    payload.get("call_id"),
                    exc_info=True,
                )
                self._publish_failure_callback(payload, context)
            finally:
                self.queue.task_done()

    async def _upload_recording(self, payload: Mapping[str, Any]) -> None:
        if self.recording_uploader is None:
            return
        try:
            await self.recording_uploader.upload_from_call_result(payload)
        except Exception:
            LOGGER.warning(
                "recording_upload_failed call_id=%s",
                payload.get("call_id"),
                exc_info=True,
            )

    def _publish_success_callback(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        message: str = "外呼完成，转写已写入",
    ) -> None:
        if self.flow_callback_writer is None:
            return
        try:
            event = build_flow_callback_event(
                context,
                status="SUCCESS",
                message=message,
                business_id=_prompt_text(payload.get("business_id")),
            )
            if event is not None:
                self.flow_callback_writer.publish(event)
        except Exception:
            LOGGER.warning(
                "flow_callback_success_publish_failed call_id=%s",
                payload.get("call_id"),
                exc_info=True,
            )

    def _publish_failure_callback(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        message: str = "转写写入失败",
    ) -> None:
        if self.flow_callback_writer is None:
            return
        try:
            event = build_flow_callback_event(
                context,
                status="FAILED",
                message=message,
                business_id=_prompt_text(payload.get("business_id")),
            )
            if event is not None:
                self.flow_callback_writer.publish(event)
        except Exception:
            LOGGER.warning(
                "flow_callback_transcript_failure_publish_failed call_id=%s",
                payload.get("call_id"),
                exc_info=True,
            )


def _call_result_failure_message(payload: Mapping[str, Any]) -> str:
    reason = _prompt_text(payload.get("failure_reason"))
    if reason:
        return f"外呼失败：{reason}"
    return "外呼失败"


def build_call_record_transcript_json(payload: Mapping[str, Any]) -> str:
    turns = []
    raw_turns = payload.get("turns")
    if isinstance(raw_turns, list):
        for item in raw_turns:
            if not isinstance(item, Mapping):
                continue
            role = _prompt_text(item.get("role"))
            if role not in {"assistant", "user"}:
                continue
            text = _prompt_text(item.get("text"))
            if not text:
                continue
            turn = {"role": role, "text": text}
            speaker_type = _prompt_text(item.get("speaker_type"))
            if speaker_type:
                turn["speaker_type"] = speaker_type
            agent_id = _prompt_text(item.get("agent_id"))
            if agent_id:
                turn["agent_id"] = agent_id
            start_ms = _transcript_int(item.get("start_ms"))
            if start_ms is not None:
                turn["start_ms"] = start_ms
            end_ms = _transcript_int(item.get("end_ms"))
            if end_ms is not None:
                turn["end_ms"] = end_ms
            confidence = _transcript_float(item.get("confidence"))
            if confidence is not None:
                turn["confidence"] = confidence
            turns.append(turn)
    return json.dumps({"turns": turns}, ensure_ascii=False)


def _transcript_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _transcript_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ThreadsafeBusinessPromptPreparer:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        store: AsyncBusinessPromptStoreProtocol,
        *,
        fallback_instructions: str,
        timeout_seconds: float,
    ) -> None:
        self.loop = loop
        self.store = store
        self.fallback_instructions = fallback_instructions
        self.timeout_seconds = timeout_seconds

    def prepare(self, context: Mapping[str, Any]) -> BusinessPromptPreparation | None:
        for attempt in range(2):
            future = asyncio.run_coroutine_threadsafe(
                self.store.prepare_business_prompt(
                    context,
                    fallback_instructions=self.fallback_instructions,
                ),
                self.loop,
            )
            try:
                return future.result(timeout=self.timeout_seconds)
            except FutureTimeoutError:
                future.cancel()
                LOGGER.warning("business_prompt_prepare_timeout", exc_info=True)
                return None
            except Exception:
                if attempt == 0:
                    LOGGER.warning("business_prompt_prepare_retry", exc_info=True)
                    continue
                LOGGER.warning("business_prompt_prepare_failed", exc_info=True)
                return None
        return None


class PostgresRuntime:
    """Owns optional PostgreSQL connectivity for business prompt snapshots."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        fallback_instructions: str,
        flow_callback_writer: FlowCallbackWriterProtocol | None = None,
    ) -> None:
        self.gateway_config = config
        self.config = config.postgres
        self.fallback_instructions = fallback_instructions
        self.flow_callback_writer = flow_callback_writer
        self.pool: Any | None = None
        self.prompt_store: PostgresPromptStore | None = None
        self.call_destination_store: PostgresCallDestinationStore | None = None
        self.call_destination_resolver: ThreadsafeCallDestinationResolver | None = None
        self.call_record_store: PostgresCallRecordStore | None = None
        self.call_record_updater: ThreadsafeCallRecordUpdater | None = None
        self.recording_uploader: RecordingUploadService | None = None
        self.call_result_writer: PostgresCallResultWriter | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            return

        dsn = os.getenv(self.config.dsn_env)
        if not dsn:
            LOGGER.warning(
                "postgres_disabled_missing_dsn dsn_env=%s",
                self.config.dsn_env,
            )
            return

        try:
            asyncpg = _load_asyncpg()
            self.pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=self.config.min_pool_size,
                max_size=self.config.max_pool_size,
                command_timeout=self.config.command_timeout_seconds,
                max_inactive_connection_lifetime=0,
                server_settings={"application_name": POSTGRES_APPLICATION_NAME},
            )
        except Exception:
            LOGGER.warning("postgres_pool_start_failed", exc_info=True)
            self.pool = None
            return

        LOGGER.info(
            "postgres_runtime_started min_pool_size=%s max_pool_size=%s "
            "prompt_query_wiring=enabled call_record_update_wiring=enabled",
            self.config.min_pool_size,
            self.config.max_pool_size,
        )
        self.prompt_store = PostgresPromptStore(self.pool)
        self.call_destination_store = PostgresCallDestinationStore(self.pool)
        self.call_destination_resolver = ThreadsafeCallDestinationResolver(
            asyncio.get_running_loop(),
            self.call_destination_store,
            timeout_seconds=self.config.command_timeout_seconds,
        )
        self.call_record_store = PostgresCallRecordStore(self.pool)
        self.call_record_updater = ThreadsafeCallRecordUpdater(
            asyncio.get_running_loop(),
            self.call_record_store,
            timeout_seconds=self.config.command_timeout_seconds,
        )
        self.recording_uploader = RecordingUploadService(
            self.call_record_store,
            MinioRecordingStorage(
                timeout_seconds=(
                    self.gateway_config.call_recording.upload_timeout_seconds
                )
            ),
            self.gateway_config.call_recording,
        )
        self.call_result_writer = PostgresCallResultWriter(
            self.call_record_store,
            flow_callback_writer=self.flow_callback_writer,
            recording_uploader=self.recording_uploader,
        )
        self.call_result_writer.start()

    async def stop(self) -> None:
        if self.call_result_writer is not None:
            await self.call_result_writer.stop()
        if self.pool is not None:
            with contextlib.suppress(Exception):
                await self.pool.close()
        self.pool = None
        self.prompt_store = None
        self.call_destination_store = None
        self.call_destination_resolver = None
        self.call_record_store = None
        self.call_record_updater = None
        self.recording_uploader = None
        self.call_result_writer = None


def fallback_prompt_snapshot(scene: str, instructions: str) -> PromptSnapshot:
    return PromptSnapshot(
        scene=scene,
        version="fallback",
        instructions=instructions,
        content_hash=_hash_text(instructions),
        loaded_at_ms=_now_ms(),
        metadata={"source": "fallback"},
    )


def _load_asyncpg() -> Any:
    try:
        import asyncpg  # type: ignore[import-not-found]
    except ModuleNotFoundError as err:
        raise RuntimeError(
            "asyncpg is required when postgres.enabled=true. "
            "Install project dependencies before enabling PostgreSQL."
        ) from err
    return asyncpg


def _business_prompt_params(
    context: Mapping[str, Any],
) -> tuple[str, int] | None:
    identity_name = _context_text(context.get("identityName"))
    debt_id = _context_int(context.get("debtId"))
    if identity_name is None or debt_id is None:
        return None
    return identity_name, debt_id


def _business_call_record_params(
    context: Mapping[str, Any],
) -> BusinessCallRecordRef | None:
    call_id = _context_int(context.get("callId"))
    debt_id = _context_int(context.get("debtId"))
    if call_id is None or debt_id is None:
        return None
    return BusinessCallRecordRef(call_id=call_id, debt_id=debt_id)


def _is_local_outbound_test_context(context: Mapping[str, Any]) -> bool:
    return _context_text(context.get("scene")) == LOCAL_OUTBOUND_TEST_SCENE


def _historical_summary_params(
    context: Mapping[str, Any],
) -> tuple[int, str | None, int] | None:
    debt_id = _context_int(context.get("debtId"))
    call_id = _context_int(context.get("callId"))
    if debt_id is None or call_id is None:
        return None
    return debt_id, _context_text(context.get("tenantId")), call_id


async def _load_historical_call_summaries(
    conn: Any,
    context: Mapping[str, Any],
) -> list[HistoricalCallSummary]:
    params = _historical_summary_params(context)
    if params is None:
        return []
    debt_id, tenant_id, call_id = params
    try:
        rows = await conn.fetch(
            CALL_RECORD_HISTORICAL_SUMMARIES_SQL,
            debt_id,
            tenant_id,
            call_id,
        )
    except Exception:
        LOGGER.warning("historical_call_summary_lookup_failed", exc_info=True)
        return []

    summaries: list[HistoricalCallSummary] = []
    for row in rows:
        summary = _analysis_result_summary(_row_value(row, "analysis_result"))
        if not summary:
            continue
        summaries.append(
            HistoricalCallSummary(
                call_id=_prompt_text(_row_value(row, "id")),
                summary=_truncate_summary(summary, HISTORICAL_SUMMARY_MAX_CHARS),
            )
        )
    return summaries


def _analysis_result_summary(value: object) -> str | None:
    if isinstance(value, Mapping):
        data = value
    else:
        text = _context_text(value)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("historical_call_summary_skipped_invalid_json")
            return None
    if not isinstance(data, Mapping):
        return None
    return _context_text(data.get("summary"))


def _truncate_summary(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return value[: max_chars - 3] + "..."


def _render_history_summary_block(
    summaries: list[HistoricalCallSummary],
    *,
    debt_amount: object = None,
    address: object = None,
) -> str:
    if not summaries:
        return ""
    header_lines = [
        "# 历史外呼摘要",
        "以下内容来自同一债务记录的历史外呼分析，仅作为业务背景。",
        "这些摘要按历史通话时间排列，不标注历史外呼身份。",
        "这不代表用户本轮刚刚表达这些内容，也不代表本轮已完成身份核实。",
        "历史摘要不是用户本轮最新表达的内容来源。",
        "除非用户在本轮明确重新说出，绝不能把历史摘要里的投诉点、服务问题、费用态度说成用户刚才提到。",
        "不得说“您刚才提到/您提到电梯、卫生等问题”；只能在用户本轮重新提出相关问题后，再结合历史作为背景回应。",
        "本轮必须按当前外呼身份和当前阶段策略重新核实身份，再根据用户最新回答推进。",
        "不得根据历史第几通推断当时外呼身份；历史摘要只用于理解客户过往态度、诉求、承诺和争议点。",
        "",
    ]
    lines = list(header_lines)
    remaining_chars = HISTORICAL_SUMMARY_BLOCK_MAX_CHARS - len("\n".join(lines))
    for index, summary in enumerate(summaries, start=1):
        redacted_summary = _redact_summary_sensitive_details(
            summary.summary,
            debt_amount=debt_amount,
            address=address,
        )
        item = f"{index}. 历史第{index}通：{redacted_summary}"
        if len(item) + 1 > remaining_chars:
            break
        lines.append(item)
        remaining_chars -= len(item) + 1
    if len(lines) == len(header_lines):
        return ""
    return "\n".join(lines)


def _redact_summary_sensitive_details(
    summary: str,
    *,
    debt_amount: object = None,
    address: object = None,
) -> str:
    text = summary
    address_text = _prompt_text(address)
    if address_text:
        text = text.replace(address_text, "[地址已隐藏]")
    amount_text = _prompt_text(debt_amount)
    if amount_text:
        text = text.replace(f"{amount_text}元", "[金额已隐藏]")
        text = text.replace(amount_text, "[金额已隐藏]")
    return re.sub(r"\d+(?:\.\d+)?\s*元", "[金额已隐藏]", text)


def _context_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _context_int(value: object) -> int | None:
    text = _context_text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key)


def _call_record_precheck_passed(
    row: Any | None,
    params: BusinessCallRecordRef,
    *,
    allowed_statuses: set[str],
) -> bool:
    if row is None:
        LOGGER.warning("call_record_update_skipped_missing callId=%s", params.call_id)
        return False

    debt_id = _context_int(_row_value(row, "debt_id"))
    if debt_id != params.debt_id:
        LOGGER.warning(
            "call_record_update_skipped_debt_mismatch callId=%s expectedDebtId=%s",
            params.call_id,
            params.debt_id,
        )
        return False

    status = _prompt_text(_row_value(row, "status"))
    if status in CALL_RECORD_TERMINAL_STATUSES:
        LOGGER.warning(
            "call_record_update_skipped_terminal callId=%s status=%s",
            params.call_id,
            status,
        )
        return False
    if status not in allowed_statuses:
        LOGGER.warning(
            "call_record_update_skipped_status callId=%s status=%s",
            params.call_id,
            status,
        )
        return False
    return True


def _execute_updated_row(result: object) -> bool:
    if not isinstance(result, str):
        return False
    parts = result.split()
    return bool(parts and parts[-1] != "0")


def _execute_inserted_row(result: object) -> bool:
    if not isinstance(result, str):
        return False
    return result.startswith("INSERT")


def _oss_config_from_row(row: Any) -> OssConfig:
    region = _context_text(_row_value(row, "region")) or "us-east-1"
    return OssConfig(
        endpoint=_context_text(_row_value(row, "endpoint")) or "",
        bucket_name=_context_text(_row_value(row, "bucket_name")) or "",
        access_key=_context_text(_row_value(row, "access_key")) or "",
        secret_key=_context_text(_row_value(row, "secret_key")) or "",
        prefix=_context_text(_row_value(row, "prefix")) or "",
        is_https=_oss_config_bool(_row_value(row, "is_https")),
        region=region,
        domain=_context_text(_row_value(row, "domain")) or "",
    )


def _oss_config_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = _context_text(value)
    if text is None:
        return False
    return text.lower() in {"1", "true", "y", "yes"}


def _voice_selection_from_row(row: Any | None) -> VoiceSelection | None:
    if row is None:
        return None
    speaker = _prompt_text(_row_value(row, "base_voice_id"))
    if not speaker:
        return None
    voice_id = _prompt_text(_row_value(row, "selected_voice_id"))
    voice_name = _prompt_text(_row_value(row, "voice_name")) or speaker
    return VoiceSelection(
        voice_id=voice_id,
        voice_name=voice_name,
        speaker=speaker,
        gender_match=_prompt_text(_row_value(row, "gender_match")),
        employee_name=_prompt_text(_row_value(row, "employee_name")),
        selected_gender=_prompt_text(_row_value(row, "selected_gender")),
    )


def _render_business_prompt(
    *,
    employee_name: object,
    strategy: object,
    speaking_style: object = None,
    debtor_name: object,
    debtor_gender: object,
    debtor_age: object,
    debt_amount: object,
    address: object,
    history_summary_block: str | None = None,
) -> str:
    salutation = _prompt_debtor_salutation(debtor_name, debtor_gender)
    speaking_style_text = _prompt_block(speaking_style) or BUSINESS_DIALOG_SPEAKING_STYLE
    lines = [
        "# 角色",
        f"你是{_prompt_text(employee_name)}，负责通过电话进行合规的逾期费用提醒和费用处理沟通。",
        "",
        "# 催收策略",
        _prompt_block(strategy),
        "",
        "# 客服语气配置",
        speaking_style_text,
        "",
        "# 规则优先级",
        *numbered_business_rule_priority_rules(),
        "",
        "# 高优先级运行红线",
        *BUSINESS_CRITICAL_RUNTIME_RULES,
        "",
        "# 对话风格",
        *numbered_business_dialog_style_rules(),
        "",
        "# 事实边界",
        *numbered_business_fact_boundary_rules(),
        "",
        "# 身份核实与隐私边界",
        *numbered_business_privacy_disclosure_rules(),
        f"7. 身份未确认时，下一句只能问：请问您是{salutation}本人，或者是这项物业费事项的授权处理人吗？",
        "8. 这类身份核实句不得夹带地址、房号、待处理金额、欠费明细或费用原因。",
        "9. 用户抱怨啰嗦、要求直接说、追问什么事但仍未确认身份时，只能说明“为保护信息安全，确认本人或授权处理人后才能说明具体内容”，不得披露具体信息。",
        "",
        "# 身份确认后的信息边界",
        "具体金额不写入本轮对话提示词；无论身份是否确认，均不得在通话中说出具体金额。",
        "用户询问欠款金额、差多少钱或待处理金额时，不得说出系统记录金额，不得复述用户提到的金额；只能说明具体金额以物业系统或官方已公示渠道核实为准。",
        "地址、房号和费用明细不写入本轮对话提示词；用户追问地址、房号或费用构成明细时，只能说明以物业系统或官方已公示渠道核实为准。",
        "回答金额相关问题后直接收口；不得追问近期是否安排处理、是否有缴费计划或处理计划。",
        f"业主称呼：{salutation}",
        "",
    ]
    if history_summary_block:
        lines.extend([history_summary_block, ""])
    lines.extend(
        [
            "# 金额与争议处理",
            *numbered_business_amount_dispute_rules(),
            "",
            "# 物业费场景约束",
            *numbered_business_property_fee_scene_rules(),
            "",
            "# 沟通规范",
            *numbered_business_communication_norms_rules(),
        ]
    )
    return "\n".join(lines)


def _prompt_block(value: object) -> str:
    return str(value or "").strip()


def _sanitize_business_strategy_text(value: object, *, append_note: bool = True) -> str:
    text = _prompt_block(value)
    if not text:
        return ""

    kept: list[str] = []
    removed = False
    for unit in _business_strategy_units(text):
        if _business_strategy_unit_conflicts(unit):
            removed = True
            continue
        kept.append(unit)

    if kept:
        if removed and append_note:
            kept.append("已忽略与全局业务红线冲突的策略内容。")
        return _redact_prompt_amounts("\n".join(kept))
    if append_note:
        return "已忽略与全局业务红线冲突的策略内容。"
    return ""


def _redact_prompt_amounts(text: str) -> str:
    return PROMPT_AMOUNT_RE.sub("[金额已隐藏]", text)


def _business_strategy_units(text: str) -> list[str]:
    units: list[str] = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        for unit in re.split(r"(?<=[。；;])\s*", line):
            unit = unit.strip()
            if unit:
                units.append(unit)
    return units


def _business_strategy_unit_conflicts(unit: str) -> bool:
    if any(marker in unit for marker in ALLOWED_NEGATED_MARKERS):
        return False
    return any(marker in unit for marker in CONFLICTING_STRATEGY_MARKERS)


def _prompt_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return " ".join(str(value).split())


def _prompt_debtor_salutation(debtor_name: object, debtor_gender: object) -> str:
    name = _prompt_text(debtor_name)
    if not name:
        return "业主"
    gender = _prompt_text(debtor_gender)
    if gender == "男":
        title = "先生"
    elif gender == "女":
        title = "女士"
    else:
        title = "业主"
    return f"{name[0]}{title}"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)
