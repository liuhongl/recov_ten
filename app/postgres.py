from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
    numbered_business_privacy_disclosure_rules,
)
from .config import GatewayConfig
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

    def __init__(self, config: GatewayConfig, *, fallback_instructions: str) -> None:
        self.config = config.postgres
        self.fallback_instructions = fallback_instructions
        self.pool: Any | None = None
        self.prompt_store: PostgresPromptStore | None = None
        self.call_result_writer: None = None

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
            "prompt_query_wiring=enabled call_result_insert_wiring=disabled",
            self.config.min_pool_size,
            self.config.max_pool_size,
        )
        self.prompt_store = PostgresPromptStore(self.pool)

    async def stop(self) -> None:
        if self.pool is not None:
            with contextlib.suppress(Exception):
                await self.pool.close()
        self.pool = None
        self.prompt_store = None


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
            f"你是{_prompt_text(employee_name)}，负责通过电话进行合规的逾期费用提醒和还款沟通。",
            "",
            "# 催收策略",
            _prompt_block(strategy),
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
            "# 沟通规范",
            "1. 只围绕逾期费用提醒、身份确认、还款意愿、还款安排进行沟通。",
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
