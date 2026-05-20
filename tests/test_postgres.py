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
    class Pool:
        async def close(self):
            pass

    class FakeAsyncpg:
        async def create_pool(self, **kwargs):
            return Pool()

    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(postgres, "_load_asyncpg", lambda: FakeAsyncpg())

    asyncio.run(_assert_runtime_success_creates_prompt_store())


def test_postgres_prompt_store_prepares_business_prompt_from_context():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_identity_name" in query:
                assert args == ("collector-a",)
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                assert args == ("collector-a", 3)
                return {"strategy_core": "先确认本人，再说明费用。"}
            if "from debt_record" in query:
                assert args == (2049810626160668673,)
                return {
                    "debtor_name": "测试业主",
                    "address": "测试小区一号楼",
                    "debt_amount": "12.34",
                    "debtor_gender": "女",
                    "debtor_age": 38,
                }
            raise AssertionError(query)

    store = PostgresPromptStore(FakePool(Conn()))

    prep = asyncio.run(
        store.prepare_business_prompt(
            {
                "identityName": "collector-a",
                "personaId": "3",
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
    assert "业主姓名：测试业主" in prep.prompt_snapshot.instructions
    assert "逾期金额：12.34" in prep.prompt_snapshot.instructions
    assert prep.prompt_snapshot.metadata["source"] == "postgres"
    assert prep.prompt_snapshot.metadata["identityName"] == "collector-a"
    assert prep.prompt_snapshot.metadata["personaId"] == "3"
    assert prep.prompt_snapshot.metadata["debtId"] == "2049810626160668673"
    assert prep.opening.opening_text.startswith("您好，请问是测试业主女士吗？我是李经理。")


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
