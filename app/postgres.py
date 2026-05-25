from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .business_dialog_style import (
    numbered_business_amount_dispute_rules,
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

LOGGER = logging.getLogger(__name__)
POSTGRES_APPLICATION_NAME = "recov_ten_gateway"

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
  transcript
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

CALL_RECORD_TERMINAL_STATUSES = {"2", "3", "4"}


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


class AsyncBusinessPromptStoreProtocol(Protocol):
    async def prepare_business_prompt(
        self,
        context: Mapping[str, Any],
        *,
        fallback_instructions: str,
    ) -> BusinessPromptPreparation | None: ...


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

        employee_name = (
            voice_selection.employee_name
            if voice_selection is not None
            else _row_value(identity_row, "name")
        )
        strategy = _row_value(strategy_row, "strategy_core")
        speaking_style = _row_value(strategy_row, "speaking_style")
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
            debtor_name=debtor_name,
            debtor_gender=debtor_gender,
            debtor_age=debtor_age,
            debt_amount=debt_amount,
            address=address,
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
    ) -> None:
        self.store = store
        self.flow_callback_writer = flow_callback_writer
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
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

    def enqueue_nowait(self, payload: dict) -> bool:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            return False
        return True

    async def _run(self) -> None:
        while True:
            payload = await self.queue.get()
            try:
                transcript_json = build_call_record_transcript_json(payload)
                context = payload.get("context")
                if not isinstance(context, Mapping):
                    context = {}
                updated = await self.store.mark_transcript_completed(
                    context,
                    transcript_json,
                )
                if not updated:
                    LOGGER.warning(
                        "call_record_transcript_update_noop call_id=%s",
                        payload.get("call_id"),
                    )
                else:
                    self._publish_success_callback(payload, context)
            except Exception:
                LOGGER.warning(
                    "call_record_transcript_update_failed call_id=%s",
                    payload.get("call_id"),
                    exc_info=True,
                )
            finally:
                self.queue.task_done()

    def _publish_success_callback(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        if self.flow_callback_writer is None:
            return
        try:
            event = build_flow_callback_event(
                context,
                status="SUCCESS",
                message="外呼完成，转写已写入",
                business_id=_prompt_text(payload.get("call_id")),
            )
            if event is not None:
                self.flow_callback_writer.publish(event)
        except Exception:
            LOGGER.warning(
                "flow_callback_success_publish_failed call_id=%s",
                payload.get("call_id"),
                exc_info=True,
            )


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
            turns.append({"role": role, "text": text})
    return json.dumps({"turns": turns}, ensure_ascii=False)


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
            LOGGER.warning("business_prompt_prepare_failed", exc_info=True)
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
        self.config = config.postgres
        self.fallback_instructions = fallback_instructions
        self.flow_callback_writer = flow_callback_writer
        self.pool: Any | None = None
        self.prompt_store: PostgresPromptStore | None = None
        self.call_destination_store: PostgresCallDestinationStore | None = None
        self.call_destination_resolver: ThreadsafeCallDestinationResolver | None = None
        self.call_record_store: PostgresCallRecordStore | None = None
        self.call_record_updater: ThreadsafeCallRecordUpdater | None = None
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
        self.call_result_writer = PostgresCallResultWriter(
            self.call_record_store,
            flow_callback_writer=self.flow_callback_writer,
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
    debtor_name: object,
    debtor_gender: object,
    debtor_age: object,
    debt_amount: object,
    address: object,
) -> str:
    salutation = _prompt_debtor_salutation(debtor_name, debtor_gender)
    return "\n".join(
        [
            "# 角色",
            f"你是{_prompt_text(employee_name)}，负责通过电话进行合规的逾期费用提醒和费用处理沟通。",
            "",
            "# 催收策略",
            _prompt_block(strategy),
            "",
            "# 规则优先级",
            *numbered_business_rule_priority_rules(),
            "",
            "# 对话风格",
            *numbered_business_dialog_style_rules(),
            "",
            "# 事实边界",
            *numbered_business_fact_boundary_rules(),
            "",
            "# 身份核实与隐私边界",
            *numbered_business_privacy_disclosure_rules(),
            f"7. 身份未确认时，下一句只能问：请问您是{salutation}本人，或方便处理这项物业费事项的授权处理人吗？",
            "8. 这类身份核实句不得夹带地址、房号、待处理金额、欠费明细或费用原因。",
            "",
            "# 身份确认后才可使用的信息",
            "以下信息即使系统已知，身份确认前也禁止说出；只有用户明确确认本人或授权处理人后才可用于沟通。",
            f"业主称呼：{salutation}",
            f"性别：{_prompt_text(debtor_gender)}",
            f"年龄：{_prompt_text(debtor_age)}",
            f"系统记录待处理金额：{_prompt_text(debt_amount)}",
            f"地址：{_prompt_text(address)}",
            "",
            "# 金额与争议处理",
            *numbered_business_amount_dispute_rules(),
            "",
            "# 物业费场景约束",
            *numbered_business_property_fee_scene_rules(),
            "",
            "# 沟通规范",
            "1. 只围绕逾期费用提醒、身份确认、缴费意愿、费用处理安排进行沟通。",
            "2. 用户询问无关内容时，简短回应并礼貌拉回当前逾期费用事项。",
            "3. 不得威胁、辱骂、施压、冒充司法或公权力机构。",
            "4. 不得向非本人透露欠款金额、地址等隐私信息。",
            "5. 如果用户表示不是本人，应先确认是否方便转告，不得继续披露债务细节。",
        ]
    )


def _prompt_block(value: object) -> str:
    return str(value or "").strip()


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
