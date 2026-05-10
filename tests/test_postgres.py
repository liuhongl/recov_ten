from __future__ import annotations

import asyncio

from app.config import GatewayConfig, PostgresConfig
from app import postgres
from app.postgres import PostgresRuntime, fallback_prompt_snapshot


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
