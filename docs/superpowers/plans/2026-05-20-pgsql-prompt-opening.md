# PostgreSQL Business Prompt Opening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build方案 C: create-call time PostgreSQL business lookup, fixed prompt snapshot, and matching opening audio before outbound originate.

**Architecture:** Keep PostgreSQL I/O async on the main asyncio loop, and expose a synchronous adapter for the existing synchronous `/calls` handler. The outbound call record owns the per-call prompt snapshot, and the realtime media gateway retrieves that snapshot by `call_id` instead of querying PostgreSQL after answer.

**Tech Stack:** Python 3.11+, `asyncpg`, dataclasses, pytest, existing FreeSWITCH outbound manager and Doubao S2S opening generator.

---

## File Structure

- Modify `app/opening.py`: add database-backed opening request builder using debtor fields and employee name.
- Modify `app/postgres.py`: add business prompt dataclasses, SQL queries, renderer, `PostgresPromptStore`, and thread-safe sync adapter.
- Modify `app/call_control.py`: accept a business prompt preparer, bind prompt snapshot and generated opening to the call record before originate.
- Modify `app/realtime_phone_gateway.py`: accept a prompt snapshot provider keyed by `call_id`; prefer prebuilt snapshots over legacy prompt store lookup.
- Modify `app/main.py`: wire `PostgresRuntime.prompt_store` into outbound call creation and realtime gateway.
- Modify `tests/test_opening.py`: cover business opening text and gender title mapping.
- Modify `tests/test_postgres.py`: cover prompt store rendering, missing context fallback, runtime wiring.
- Modify `tests/test_call_control.py`: cover create-call-time prompt preparation and opening generation.
- Modify `tests/test_realtime_phone_gateway.py`: cover call-record prompt snapshot usage.

## Task 1: Business Opening Builder

**Files:**
- Modify: `app/opening.py`
- Test: `tests/test_opening.py`

- [ ] **Step 1: Write failing tests**

Add these imports in `tests/test_opening.py`:

```python
from app.opening import (
    OpeningAudio,
    OpeningAudioStore,
    OpeningGenerationFailed,
    build_business_opening_request,
    build_prepared_opening_audio,
    parse_opening_request,
)
```

Add tests:

```python
def test_build_business_opening_request_renders_employee_and_debt_snapshot():
    opening = build_business_opening_request(
        employee_name="李经理",
        debtor_name="测试业主",
        debtor_gender="男",
        debt_amount="12.34",
        address="测试小区一号楼",
    )

    assert opening.voice == "female"
    assert opening.speaker == "zh_female_vv_jupiter_bigtts"
    assert opening.business == {
        "employee_name": "李经理",
        "debtor_name": "测试业主",
        "debtor_gender": "男",
        "debt_amount": "12.34",
        "address": "测试小区一号楼",
        "title": "先生",
    }
    assert opening.opening_text == (
        "您好，请问是测试业主先生吗？我是李经理。"
        "这边来电是想和您确认一下测试小区一号楼相关的逾期费用，"
        "目前系统显示待处理金额为12.34元，方便和您核实一下吗？"
    )
    assert len(opening.opening_text_hash) == 64


def test_build_business_opening_request_uses_empty_title_for_unknown_gender():
    opening = build_business_opening_request(
        employee_name="李经理",
        debtor_name="测试业主",
        debtor_gender="",
        debt_amount="12.34",
        address="测试小区一号楼",
    )

    assert "测试业主吗？" in opening.opening_text
    assert opening.business["title"] == ""
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_opening.py::test_build_business_opening_request_renders_employee_and_debt_snapshot tests/test_opening.py::test_build_business_opening_request_uses_empty_title_for_unknown_gender -v
```

Expected: FAIL with `ImportError` or `NameError` for `build_business_opening_request`.

- [ ] **Step 3: Implement builder**

In `app/opening.py`, add:

```python
BUSINESS_OPENING_TEMPLATE = (
    "您好，请问是{debtor_name}{title}吗？我是{employee_name}。"
    "这边来电是想和您确认一下{address}相关的逾期费用，"
    "目前系统显示待处理金额为{debt_amount}元，方便和您核实一下吗？"
)
```

Add:

```python
def build_business_opening_request(
    *,
    employee_name: object,
    debtor_name: object,
    debtor_gender: object,
    debt_amount: object,
    address: object,
    voice: str = "female",
) -> OpeningRequest:
    speaker = VOICE_SPEAKERS.get(voice)
    if speaker is None:
        raise OpeningGenerationFailed("opening.voice must be female or male")

    employee_name_text = _business_text(employee_name, "employee_name", max_length=32)
    debtor_name_text = _business_text(debtor_name, "debtor_name", max_length=32)
    gender_text = "" if debtor_gender is None else str(debtor_gender).strip()
    amount_text = _arrears_amount(debt_amount)
    address_text = _business_text(address, "address", max_length=120)
    title = _debtor_title(gender_text)
    rendered = BUSINESS_OPENING_TEMPLATE.format(
        debtor_name=debtor_name_text,
        title=title,
        employee_name=employee_name_text,
        address=address_text,
        debt_amount=amount_text,
    )
    return OpeningRequest(
        voice=voice,
        speaker=speaker,
        business={
            "employee_name": employee_name_text,
            "debtor_name": debtor_name_text,
            "debtor_gender": gender_text,
            "debt_amount": amount_text,
            "address": address_text,
            "title": title,
        },
        opening_text=rendered,
        opening_text_hash=_text_hash(rendered),
    )


def _business_text(value: object, field_name: str, *, max_length: int) -> str:
    if value is None:
        raise OpeningGenerationFailed(f"{field_name} is required")
    text = " ".join(str(value).split())
    if not text:
        raise OpeningGenerationFailed(f"{field_name} is required")
    if len(text) > max_length:
        raise OpeningGenerationFailed(f"{field_name} is too long")
    return text


def _debtor_title(gender: str) -> str:
    if gender == "男":
        return "先生"
    if gender == "女":
        return "女士"
    return ""
```

- [ ] **Step 4: Run tests and confirm pass**

Run:

```bash
pytest tests/test_opening.py -v
```

Expected: PASS.

## Task 2: PostgreSQL Prompt Store

**Files:**
- Modify: `app/postgres.py`
- Test: `tests/test_postgres.py`

- [ ] **Step 1: Write failing tests**

Add imports:

```python
from app.postgres import (
    BusinessPromptPreparation,
    PostgresPromptStore,
    PostgresRuntime,
    ThreadsafeBusinessPromptPreparer,
    fallback_prompt_snapshot,
)
```

Add fake pool helpers:

```python
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
```

Add prompt store test:

```python
def test_postgres_prompt_store_prepares_business_prompt_from_context():
    class Conn:
        async def fetchrow(self, query, *args):
            if "from call_identity_name" in query:
                assert args == ("collector-a",)
                return {"name": "李经理"}
            if "from persona_call_strategy" in query:
                assert args == ("collector-a", "persona-1")
                return {"strategy_core": "先确认本人，再说明费用。"}
            if "from debt_record" in query:
                assert args == ("debt-1",)
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
                "personaId": "persona-1",
                "debtId": "debt-1",
            },
            fallback_instructions="fallback",
        )
    )

    assert prep is not None
    assert isinstance(prep, BusinessPromptPreparation)
    assert prep.prompt_snapshot.scene == "collector-a:persona-1"
    assert prep.prompt_snapshot.version == "postgres"
    assert "你是李经理" in prep.prompt_snapshot.instructions
    assert "先确认本人，再说明费用。" in prep.prompt_snapshot.instructions
    assert "业主姓名：测试业主" in prep.prompt_snapshot.instructions
    assert prep.prompt_snapshot.metadata["source"] == "postgres"
    assert prep.prompt_snapshot.metadata["identityName"] == "collector-a"
    assert prep.opening.opening_text.startswith("您好，请问是测试业主女士吗？我是李经理。")
```

Add missing-context test:

```python
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
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_postgres.py::test_postgres_prompt_store_prepares_business_prompt_from_context tests/test_postgres.py::test_postgres_prompt_store_returns_none_when_business_context_missing -v
```

Expected: FAIL because `PostgresPromptStore` and related classes do not exist.

- [ ] **Step 3: Implement prompt store**

In `app/postgres.py`, import:

```python
from collections.abc import Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from decimal import Decimal
from typing import Protocol

from .opening import OpeningRequest, build_business_opening_request
```

Add:

```python
@dataclass(frozen=True)
class BusinessPromptPreparation:
    prompt_snapshot: PromptSnapshot
    opening: OpeningRequest
```

Add SQL constants:

```python
IDENTITY_NAME_SQL = """
select name
from call_identity_name
where identity_name = $1
order by random()
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
```

Add `PostgresPromptStore` with:

```python
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
        identity_name, persona_id, debt_id = params
        async with self.pool.acquire() as conn:
            identity_row = await conn.fetchrow(IDENTITY_NAME_SQL, identity_name)
            strategy_row = await conn.fetchrow(STRATEGY_SQL, identity_name, persona_id)
            debt_row = await conn.fetchrow(DEBT_RECORD_SQL, debt_id)
        if identity_row is None or strategy_row is None or debt_row is None:
            LOGGER.warning(
                "business_prompt_lookup_missing identityName=%s personaId=%s debtId=%s "
                "has_identity=%s has_strategy=%s has_debt=%s",
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
        opening = build_business_opening_request(
            employee_name=employee_name,
            debtor_name=debtor_name,
            debtor_gender=debtor_gender,
            debt_amount=debt_amount,
            address=address,
        )
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
                    "personaId": persona_id,
                    "debtId": debt_id,
                    "employee_name": str(employee_name),
                    "opening_text_hash": opening.opening_text_hash,
                },
            ),
            opening=opening,
        )
```

Add helpers `_business_prompt_params`, `_row_value`, `_render_business_prompt`, and `_prompt_text` that normalize whitespace and format `Decimal` values without scientific notation.

- [ ] **Step 4: Wire runtime store**

In `PostgresRuntime.start`, after creating the pool:

```python
self.prompt_store = PostgresPromptStore(self.pool)
```

Change `PostgresRuntime.__init__` type:

```python
self.prompt_store: PostgresPromptStore | None = None
```

- [ ] **Step 5: Run tests and confirm pass**

Run:

```bash
pytest tests/test_postgres.py -v
```

Expected: PASS.

## Task 3: Synchronous Business Prompt Preparation for Outbound Calls

**Files:**
- Modify: `app/postgres.py`
- Test: `tests/test_postgres.py`

- [ ] **Step 1: Write failing adapter test**

Add:

```python
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
        result = await asyncio.to_thread(preparer.prepare, {"identityName": "collector-a"})
        assert result == "prepared"

    asyncio.run(assert_preparer())
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
pytest tests/test_postgres.py::test_threadsafe_business_prompt_preparer_runs_store_on_event_loop -v
```

Expected: FAIL because `ThreadsafeBusinessPromptPreparer` does not exist.

- [ ] **Step 3: Implement adapter**

In `app/postgres.py`, add:

```python
class AsyncBusinessPromptStoreProtocol(Protocol):
    async def prepare_business_prompt(
        self,
        context: Mapping[str, Any],
        *,
        fallback_instructions: str,
    ) -> BusinessPromptPreparation | None: ...


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
```

- [ ] **Step 4: Run tests and confirm pass**

Run:

```bash
pytest tests/test_postgres.py::test_threadsafe_business_prompt_preparer_runs_store_on_event_loop -v
```

Expected: PASS.

## Task 4: Bind Business Prompt and Opening to Outbound Call Record

**Files:**
- Modify: `app/call_control.py`
- Test: `tests/test_call_control.py`

- [ ] **Step 1: Write failing tests**

Add import:

```python
from app.postgres import BusinessPromptPreparation, PromptSnapshot
```

Add test:

```python
def test_outbound_manager_prepares_business_prompt_and_opening_before_originating():
    events: list[str] = []
    store = OpeningAudioStore()

    snapshot = PromptSnapshot(
        scene="collector-a:persona-1",
        version="postgres",
        instructions="业务提示词",
        content_hash="hash-prompt",
        loaded_at_ms=123,
        metadata={"source": "postgres"},
    )

    class FakeBusinessPromptPreparer:
        def prepare(self, context):
            events.append("prepare_prompt")
            assert context == {
                "identityName": "collector-a",
                "personaId": "persona-1",
                "debtId": "debt-1",
            }
            opening = parse_opening_request(
                {
                    "voice": "female",
                    "business": {
                        "owner_name": "测试业主",
                        "arrears_amount": "12.34",
                    },
                }
            )
            assert opening is not None
            return BusinessPromptPreparation(snapshot, opening)

    class FakeOpeningGenerator:
        def generate(self, opening):
            events.append("generate_opening")
            assert opening.opening_text_hash
            return OpeningAudio(
                pcm16=samples_to_pcm_s16le([1200] * 480),
                sample_rate=24000,
                generation_ms=1200,
            )

    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            events.append("originate")
            return "+OK call accepted"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(event_socket=EventSocketConfig(enabled=True)),
        dialer_factory=lambda: FakeDialer(),
        opening_generator=FakeOpeningGenerator(),
        opening_store=store,
        business_prompt_preparer=FakeBusinessPromptPreparer(),
    )

    try:
        call = manager.create_call(
            {
                "destination": "1000",
                "context": {
                    "identityName": "collector-a",
                    "personaId": "persona-1",
                    "debtId": "debt-1",
                },
            }
        )

        assert call["prompt"]["content_hash"] == "hash-prompt"
        assert "instructions" not in call["prompt"]
        assert call["opening"]["status"] == "ready"
        assert manager.get_prompt_snapshot(call["call_id"]) is snapshot
        _wait_for_status(manager, call["call_id"], "originated")
        assert events == ["prepare_prompt", "generate_opening", "originate"]
    finally:
        manager.shutdown()
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
pytest tests/test_call_control.py::test_outbound_manager_prepares_business_prompt_and_opening_before_originating -v
```

Expected: FAIL because `business_prompt_preparer`, `prompt`, and `get_prompt_snapshot` do not exist.

- [ ] **Step 3: Implement manager integration**

In `app/call_control.py`, import:

```python
from .postgres import BusinessPromptPreparation, PromptSnapshot
```

Add protocol:

```python
class BusinessPromptPreparerProtocol(Protocol):
    def prepare(self, context: dict[str, Any]) -> BusinessPromptPreparation | None: ...
```

Add field to `OutboundCallRecord`:

```python
prompt_snapshot: PromptSnapshot | None = None
```

Add safe prompt summary in `to_dict`:

```python
"prompt": None if self.prompt_snapshot is None else {
    "scene": self.prompt_snapshot.scene,
    "version": self.prompt_snapshot.version,
    "content_hash": self.prompt_snapshot.content_hash,
    "loaded_at_ms": self.prompt_snapshot.loaded_at_ms,
    "metadata": self.prompt_snapshot.metadata,
},
```

Update manager constructor:

```python
business_prompt_preparer: BusinessPromptPreparerProtocol | None = None,
```

Store it on `self._business_prompt_preparer`.

Add:

```python
def get_prompt_snapshot(self, call_id: str) -> PromptSnapshot | None:
    with self._lock:
        record = self._calls.get(call_id)
        return None if record is None else record.prompt_snapshot
```

In `create_call`, after `record = self._build_record(request)`:

```python
business_opening = self._prepare_business_prompt(record)
opening = business_opening or request.opening
if opening is not None:
    self._prepare_opening(record, opening)
```

Add:

```python
def _prepare_business_prompt(self, record: OutboundCallRecord) -> OpeningRequest | None:
    if self._business_prompt_preparer is None:
        return None
    preparation = self._business_prompt_preparer.prepare(record.context)
    if preparation is None:
        return None
    record.prompt_snapshot = preparation.prompt_snapshot
    LOGGER.info(
        "business_prompt_ready call_id=%s scene=%s version=%s content_hash=%s",
        record.call_id,
        preparation.prompt_snapshot.scene,
        preparation.prompt_snapshot.version,
        preparation.prompt_snapshot.content_hash,
    )
    return preparation.opening
```

- [ ] **Step 4: Run tests and confirm pass**

Run:

```bash
pytest tests/test_call_control.py -v
```

Expected: PASS.

## Task 5: Use Prebuilt Prompt Snapshot in Realtime Gateway

**Files:**
- Modify: `app/realtime_phone_gateway.py`
- Test: `tests/test_realtime_phone_gateway.py`

- [ ] **Step 1: Write failing test**

Add:

```python
def test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id():
    asyncio.run(_assert_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id())


async def _assert_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id() -> None:
    snapshot = PromptSnapshot(
        scene="collector-a:persona-1",
        version="postgres",
        instructions="业务提示词",
        content_hash="hash-prompt",
        loaded_at_ms=123,
        metadata={"source": "postgres"},
    )

    class FailingStore:
        async def get_prompt_snapshot(self, scene=None, *, fallback_instructions=None):
            raise AssertionError("legacy prompt store should not be queried")

    server = FreeSwitchRealtimeGatewayServer(
        GatewayConfig(),
        api_key="test",
        prompt_store=FailingStore(),
        prompt_snapshot_provider=lambda call_id: snapshot if call_id == "call-1" else None,
    )
    session = RealtimePhoneSessionStats(
        call_id="call-1",
        session_id="session-1",
        connected_at=1.0,
        last_seen_at=1.0,
        expected_frame_bytes=320,
    )

    loaded = await server._load_prompt_snapshot(session)

    assert loaded is snapshot
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
pytest tests/test_realtime_phone_gateway.py::test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id -v
```

Expected: FAIL because `prompt_snapshot_provider` is not accepted.

- [ ] **Step 3: Implement provider support**

In `app/realtime_phone_gateway.py`, add:

```python
PromptSnapshotProvider = Callable[[str], PromptSnapshot | None]
```

Add constructor parameter:

```python
prompt_snapshot_provider: PromptSnapshotProvider | None = None,
```

Store:

```python
self.prompt_snapshot_provider = prompt_snapshot_provider
```

In `_load_prompt_snapshot`, before checking `self.prompt_store`:

```python
if self.prompt_snapshot_provider is not None:
    try:
        snapshot = self.prompt_snapshot_provider(session.call_id)
    except Exception:
        LOGGER.warning(
            "prebuilt_prompt_snapshot_load_failed call_id=%s session_id=%s",
            session.call_id,
            session.session_id,
            exc_info=True,
        )
    else:
        if snapshot is not None:
            LOGGER.info(
                "prebuilt_prompt_snapshot_loaded call_id=%s session_id=%s scene=%s "
                "version=%s content_hash=%s",
                session.call_id,
                session.session_id,
                snapshot.scene,
                snapshot.version,
                snapshot.content_hash,
            )
            return snapshot
```

- [ ] **Step 4: Run tests and confirm pass**

Run:

```bash
pytest tests/test_realtime_phone_gateway.py::test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id -v
```

Expected: PASS.

## Task 6: Main Runtime Wiring

**Files:**
- Modify: `app/main.py`
- Test: existing integration-oriented unit tests from earlier tasks

- [ ] **Step 1: Implement wiring**

In `app/main.py`, import:

```python
from .postgres import PostgresRuntime, ThreadsafeBusinessPromptPreparer
```

In `_serve`, after `await postgres_runtime.start()`:

```python
business_prompt_preparer = None
if postgres_runtime.prompt_store is not None:
    business_prompt_preparer = ThreadsafeBusinessPromptPreparer(
        asyncio.get_running_loop(),
        postgres_runtime.prompt_store,
        fallback_instructions=DEFAULT_PHONE_INSTRUCTIONS,
        timeout_seconds=config.postgres.command_timeout_seconds,
    )
```

Pass into `OutboundCallManager`:

```python
business_prompt_preparer=business_prompt_preparer,
```

Pass snapshot provider into `FreeSwitchRealtimeGatewayServer`:

```python
prompt_snapshot_provider=outbound_manager.get_prompt_snapshot,
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
pytest tests/test_postgres.py tests/test_call_control.py tests/test_realtime_phone_gateway.py::test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id -v
```

Expected: PASS.

## Task 7: Final Verification

**Files:**
- No code edits.

- [ ] **Step 1: Run full unit tests**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 2: Check repository status**

Run:

```bash
git status --short
```

Expected: only intended files modified, plus the pre-existing `freeswitch-local/conf/vars.xml` change remains untouched.

- [ ] **Step 3: Commit implementation**

Stage only intended files:

```bash
git add app/opening.py app/postgres.py app/call_control.py app/realtime_phone_gateway.py app/main.py tests/test_opening.py tests/test_postgres.py tests/test_call_control.py tests/test_realtime_phone_gateway.py docs/superpowers/plans/2026-05-20-pgsql-prompt-opening.md
```

Commit:

```bash
git commit -m "feat: 支持 pgsql 业务提示词生成"
```

Expected: commit succeeds without using `--no-verify`.

## Self-Review

Spec coverage:

- Create-call-time PostgreSQL lookup: Task 2, Task 3, Task 4, Task 6.
- `identityName/personaId/debtId` context parameters: Task 2, Task 4.
- Prompt template rendering: Task 2.
- Opening text from the same snapshot: Task 1, Task 2, Task 4.
- Per-call fixed snapshot and no post-answer re-query: Task 4, Task 5.
- Safe logging and no prompt leak in call status API: Task 4.
- Fallback behavior: Task 2 and adapter behavior in Task 3.

Placeholder scan:

- No `TBD`, `TODO`, or unresolved placeholders.

Type consistency:

- `BusinessPromptPreparation.prompt_snapshot` is used consistently by `PostgresPromptStore`, `OutboundCallManager`, and `FreeSwitchRealtimeGatewayServer`.
- `business_prompt_preparer.prepare(context)` is synchronous by design for the existing synchronous call creation path.
