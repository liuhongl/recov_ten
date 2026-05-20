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

from .config import GatewayConfig
from .opening import (
    OpeningGenerationFailed,
    OpeningRequest,
    build_business_opening_request,
)

LOGGER = logging.getLogger(__name__)

IDENTITY_NAME_SQL = """
select name
from call_identity_name
where identity_name = $1
order by random()
limit 1
"""

IDENTITY_NAME_BY_NAME_SQL = """
select name
from call_identity_name
where identity_name = $1 and name = $2
order by id
limit 1
"""

STRATEGY_SQL = """
select strategy_core
from persona_call_strategy
where identity_name = $1 and persona_id = $2
limit 1
"""

DEBT_RECORD_SQL = """
select debtor_name, address, debt_amount, debtor_gender, debtor_age
from debt_record
where id = $1
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

        identity_name, employee_name, persona_id, debt_id = params
        async with self.pool.acquire() as conn:
            if employee_name is None:
                identity_row = await conn.fetchrow(IDENTITY_NAME_SQL, identity_name)
            else:
                identity_row = await conn.fetchrow(
                    IDENTITY_NAME_BY_NAME_SQL,
                    identity_name,
                    employee_name,
                )
            strategy_row = await conn.fetchrow(STRATEGY_SQL, identity_name, persona_id)
            debt_row = await conn.fetchrow(DEBT_RECORD_SQL, debt_id)

        if identity_row is None or strategy_row is None or debt_row is None:
            LOGGER.warning(
                "business_prompt_lookup_missing identityName=%s personaId=%s "
                "debtId=%s has_identity=%s has_strategy=%s has_debt=%s",
                identity_name,
                persona_id,
                debt_id,
                identity_row is not None,
                strategy_row is not None,
                debt_row is not None,
            )
            return None

        employee_name = _row_value(identity_row, "name")
        strategy = _row_value(strategy_row, "strategy_core")
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
        return BusinessPromptPreparation(
            prompt_snapshot=PromptSnapshot(
                scene=f"{identity_name}:{persona_id}",
                version="postgres",
                instructions=instructions,
                content_hash=_hash_text(instructions),
                loaded_at_ms=_now_ms(),
                metadata={
                    "source": "postgres",
                    "identityName": identity_name,
                    "personaId": str(persona_id),
                    "debtId": str(debt_id),
                    "employee_name": _prompt_text(employee_name),
                    "strategy_core": _prompt_text(strategy),
                    "opening_text_hash": opening.opening_text_hash,
                },
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
) -> tuple[str, str | None, int, int] | None:
    identity_name = _context_text(context.get("identityName"))
    employee_name = _context_text(context.get("employeeName"))
    persona_id = _context_int(context.get("personaId"))
    debt_id = _context_int(context.get("debtId"))
    if identity_name is None or persona_id is None or debt_id is None:
        return None
    return identity_name, employee_name, persona_id, debt_id


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
    return "\n".join(
        [
            "# 角色",
            f"你是{_prompt_text(employee_name)}，负责通过电话进行合规的逾期费用提醒和还款沟通。",
            "",
            "# 催收策略",
            _prompt_block(strategy),
            "",
            "# 业主信息",
            f"业主姓名：{_prompt_text(debtor_name)}",
            f"性别：{_prompt_text(debtor_gender)}",
            f"年龄：{_prompt_text(debtor_age)}",
            f"逾期金额：{_prompt_text(debt_amount)}",
            f"地址：{_prompt_text(address)}",
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


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)
