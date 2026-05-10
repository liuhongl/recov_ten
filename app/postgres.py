from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .config import GatewayConfig

LOGGER = logging.getLogger(__name__)


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


class PostgresRuntime:
    """Owns optional PostgreSQL connectivity without assuming table shape yet.

    The project already knows where PostgreSQL belongs in the runtime lifecycle,
    but the real schema is business-owned and will be wired in later. For now
    this class only validates that asyncpg can create and close a pool when
    enabled; no prompt query or call-result insert is performed.
    """

    def __init__(self, config: GatewayConfig, *, fallback_instructions: str) -> None:
        self.config = config.postgres
        self.fallback_instructions = fallback_instructions
        self.pool: Any | None = None
        self.prompt_store: None = None
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
            "query_insert_wiring=disabled",
            self.config.min_pool_size,
            self.config.max_pool_size,
        )

    async def stop(self) -> None:
        if self.pool is not None:
            with contextlib.suppress(Exception):
                await self.pool.close()
        self.pool = None


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


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)
