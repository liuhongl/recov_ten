# Realtime Context First Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the first-stage commercial context strategy: interrupted assistant replies are committed as full QA history, normal interruption does not hot-restart the provider session, and same-process hot restart restores context through `dialog_context`.

**Architecture:** Keep Volcengine's active S2S session as the normal multi-turn context owner. Add a local authoritative `committed_exchanges` ledger with playback-state fields, close interrupted turns locally so late audio is dropped, and pass recent committed QA as structured `dialog_context` only when creating a new S2S session. `ConversationRetrieve`, `ConversationCreate`, and `ConversationTruncate` remain outside the first-stage main path.

**Tech Stack:** Python 3.12, dataclasses, asyncio, websockets legacy client/server, pytest via `uv run --with pytest pytest`.

**Implementation Status:** Completed in this branch. Final verification:

- `uv run --with pytest pytest tests/test_doubao_s2s_realtime.py tests/test_realtime_phone_gateway.py tests/test_doubao_s2s_client.py -q` passed: 49 tests.
- `uv run --with pytest pytest tests -q` passed: 146 tests.
- `rg -n "ConversationRetrieve|ConversationCreate|ConversationTruncate|enable_conversation_truncate" app tests` returned no matches.
- `git diff --check` passed for modified source and test files.
- Post-review product decision: `input_transcripts/output_transcripts` are no longer emitted in call result payload; `committed_exchanges` is the only authoritative dialogue history.

---

## File Structure

- Modify `app/realtime_types.py`: add `RealtimeDialogContextItem` and extend `RealtimeDialogConfig` with `dialog_id` and `dialog_context`.
- Modify `app/doubao_s2s_client.py`: serialize `dialog_context` into `StartSession`, add `ClientInterrupt` event support.
- Modify `app/doubao_s2s_realtime.py`: change playback interruption from provider hot restart to `ClientInterrupt`, while keeping active response invalidation.
- Modify `app/realtime_phone_gateway.py`: upgrade `ConversationExchange`, commit interrupted turns, track closed turn IDs, drop late old audio, build `dialog_context` from committed history, and expand call result payload.
- Modify `tests/test_doubao_s2s_client.py`: cover `dialog_context` payload and `ClientInterrupt` frame.
- Modify `tests/test_doubao_s2s_realtime.py`: cover interruption sends `ClientInterrupt` and does not `FinishSession`/`StartSession`.
- Modify `tests/test_realtime_phone_gateway.py`: cover interrupted ledger entries, stale audio dropping, hot-restart `dialog_context`, and payload compatibility fields.

## Task 1: Add `dialog_context` Types and StartSession Serialization

**Files:**
- Modify: `app/realtime_types.py`
- Modify: `app/doubao_s2s_client.py`
- Test: `tests/test_doubao_s2s_client.py`

- [ ] **Step 1: Write failing tests for `dialog_context` serialization**

Replace the existing `RealtimeDialogConfig` import in `tests/test_doubao_s2s_client.py` with:

```python
from app.realtime_types import RealtimeDialogConfig, RealtimeDialogContextItem
```

Delete the existing single import `from app.realtime_types import RealtimeDialogConfig` so the file has only the combined import above.

Add this test after `test_start_session_payload_includes_dialog_identity_fields`:

```python
def test_start_session_payload_includes_dialog_context():
    payload = build_start_session_payload(
        DoubaoS2SSessionConfig(
            dialog=RealtimeDialogConfig(
                bot_name="物业中心小明",
                model="1.2.1.1",
                dialog_context=(
                    RealtimeDialogContextItem(role="user", text="你是哪边？"),
                    RealtimeDialogContextItem(
                        role="assistant",
                        text="我是物业中心小明。",
                    ),
                ),
            ),
        )
    )

    assert payload["dialog"]["dialog_context"] == [
        {"role": "user", "text": "你是哪边？"},
        {"role": "assistant", "text": "我是物业中心小明。"},
    ]
    assert payload["dialog"]["extra"] == {"model": "1.2.1.1"}
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
uv run --with pytest pytest tests/test_doubao_s2s_client.py::test_start_session_payload_includes_dialog_context -q
```

Expected: fail with an import error for `RealtimeDialogContextItem` or an assertion error because `dialog_context` is not serialized yet.

- [ ] **Step 3: Add `RealtimeDialogContextItem` and extend `RealtimeDialogConfig`**

Modify `app/realtime_types.py`:

```python
@dataclass(frozen=True)
class RealtimeDialogContextItem:
    role: str
    text: str
    timestamp: int | None = None

    def to_payload(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "role": self.role,
            "text": self.text,
        }
        if self.timestamp is not None:
            payload["timestamp"] = self.timestamp
        return payload


@dataclass(frozen=True)
class RealtimeDialogConfig:
    bot_name: str | None = None
    system_role: str | None = None
    speaking_style: str | None = None
    model: str | None = None
    dialog_id: str | None = None
    dialog_context: tuple[RealtimeDialogContextItem, ...] = ()
```

- [ ] **Step 4: Serialize `dialog_id` and `dialog_context`**

Modify `_build_dialog_payload()` in `app/doubao_s2s_client.py` so the body is:

```python
def _build_dialog_payload(config: RealtimeDialogConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config.bot_name:
        payload["bot_name"] = config.bot_name
    if config.system_role:
        payload["system_role"] = config.system_role
    if config.speaking_style:
        payload["speaking_style"] = config.speaking_style
    if config.dialog_id:
        payload["dialog_id"] = config.dialog_id
    if config.dialog_context:
        payload["dialog_context"] = [
            item.to_payload() for item in config.dialog_context
        ]
    if config.model:
        payload["extra"] = {"model": config.model}
    return payload
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run --with pytest pytest tests/test_doubao_s2s_client.py::test_start_session_payload_includes_dialog_identity_fields tests/test_doubao_s2s_client.py::test_start_session_payload_includes_dialog_context -q
```

Expected: both tests pass.

## Task 2: Add `ClientInterrupt` Event Support

**Files:**
- Modify: `app/doubao_s2s_client.py`
- Modify: `app/doubao_s2s_realtime.py`
- Test: `tests/test_doubao_s2s_client.py`
- Test: `tests/test_doubao_s2s_realtime.py`

- [ ] **Step 1: Write failing client frame test**

In `tests/test_doubao_s2s_client.py`, add `EVENT_CLIENT_INTERRUPT` to the import list from `app.doubao_s2s_client`.

Add this test near the other frame tests:

```python
def test_client_interrupt_frame_roundtrip_with_session_id():
    raw = build_json_event_frame(
        EVENT_CLIENT_INTERRUPT,
        {"session_id": "session-a"},
        session_id="session-a",
    )

    frame = parse_frame(raw)

    assert frame.event == EVENT_CLIENT_INTERRUPT
    assert frame.session_id == "session-a"
    assert frame.payload_json == {"session_id": "session-a"}
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
uv run --with pytest pytest tests/test_doubao_s2s_client.py::test_client_interrupt_frame_roundtrip_with_session_id -q
```

Expected: fail because `EVENT_CLIENT_INTERRUPT` does not exist.

- [ ] **Step 3: Add the client interrupt constant and method**

In `app/doubao_s2s_client.py`, add the event constant near the conversation/dialogue event constants:

```python
EVENT_CLIENT_INTERRUPT = 515
```

Add this method to `DoubaoS2SRealtimeSession` after `finish_session()`:

```python
    async def client_interrupt(self) -> None:
        await self._send_json_event(EVENT_CLIENT_INTERRUPT, {})
```

- [ ] **Step 4: Change server-vad interruption to avoid provider hot restart**

Modify `DoubaoS2SServerVadSession.handle_playback_interruption()` in `app/doubao_s2s_realtime.py` to invalidate the active response and send `ClientInterrupt` without `FinishSession`, `StartSession`, or context seeding:

```python
    async def handle_playback_interruption(
        self,
        *,
        interrupted_output_text: str | None = None,
    ) -> None:
        del interrupted_output_text
        async with self._session_restart_lock:
            session = self._require_session()
            self._invalidate_active_response()
            await session.client_interrupt()
```

This keeps `restart_on_interruption = False` truthful: normal playback interruption no longer hot-restarts the Volcengine session.

- [ ] **Step 5: Add realtime test that interruption does not restart session**

In `tests/test_doubao_s2s_realtime.py`, add `EVENT_CLIENT_INTERRUPT` to the import list from `app.doubao_s2s_client`.

Rename the existing wrapper:

```python
def test_doubao_s2s_server_vad_session_sends_client_interrupt_without_restart():
    asyncio.run(_assert_interruption_sends_client_interrupt_without_restart())
```

Replace `_assert_hot_restart_drops_stale_audio()` with:

```python
async def _assert_interruption_sends_client_interrupt_without_restart() -> None:
    captured = {"events": []}
    first_audio = asyncio.Event()
    audio_deltas: list[tuple[int, bytes]] = []
    turn_results: list[RealtimeTurnResult] = []
    initial_audio = _float32_audio(0.25)

    async def handler(websocket):
        frame = parse_frame(await websocket.recv())
        captured["events"].append(frame.event)
        assert frame.event == EVENT_START_CONNECTION
        await websocket.send(
            _server_json_frame(
                EVENT_CONNECTION_STARTED,
                {"ok": True},
                connect_id="conn-server",
            )
        )

        frame = parse_frame(await websocket.recv())
        captured["events"].append(frame.event)
        assert frame.event == EVENT_START_SESSION
        await websocket.send(
            _server_json_frame(
                EVENT_SESSION_STARTED,
                {"ok": True},
                session_id=frame.session_id,
            )
        )

        async for raw_message in websocket:
            frame = parse_frame(raw_message)
            captured["events"].append(frame.event)
            if frame.event == EVENT_TASK_AUDIO:
                await _send_basic_response_start(
                    websocket,
                    frame.session_id,
                    input_text="old input",
                    output_text="old output",
                    audio=initial_audio,
                )
                continue
            if frame.event == EVENT_CLIENT_INTERRUPT:
                break

    async def on_speech_started(turn_id: int) -> None:
        return None

    async def on_audio_delta(turn_id: int, audio: bytes) -> None:
        audio_deltas.append((turn_id, audio))
        first_audio.set()

    async def on_turn_completed(result: RealtimeTurnResult) -> None:
        turn_results.append(result)

    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        session = DoubaoS2SServerVadSession(
            _credentials(websocket_url=f"ws://127.0.0.1:{port}/dialogue"),
            DoubaoS2SSessionConfig(),
            on_speech_started=on_speech_started,
            on_audio_delta=on_audio_delta,
            on_turn_completed=on_turn_completed,
        )
        await session.connect()
        await session.append_audio(b"\x00\x01" * 320)
        await asyncio.wait_for(first_audio.wait(), timeout=3)
        await session.handle_playback_interruption(
            interrupted_output_text="old output"
        )
        await session.close()
    finally:
        server.close()
        await server.wait_closed()

    assert captured["events"] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_TASK_AUDIO,
        EVENT_CLIENT_INTERRUPT,
    ]
    assert EVENT_FINISH_SESSION not in captured["events"]
    assert captured["events"].count(EVENT_START_SESSION) == 1
    assert audio_deltas == [(1, float32le_to_pcm_s16le(initial_audio))]
    assert [result.status for result in turn_results] == ["cancelled"]
```

This replaces the old expectation that interruption performs `FinishSession -> StartSession -> SayHello`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run --with pytest pytest tests/test_doubao_s2s_client.py::test_client_interrupt_frame_roundtrip_with_session_id tests/test_doubao_s2s_realtime.py::test_doubao_s2s_server_vad_session_sends_client_interrupt_without_restart -q
```

Expected: both tests pass.

## Task 3: Upgrade `ConversationExchange` and Call Result Payload

**Files:**
- Modify: `app/realtime_phone_gateway.py`
- Test: `tests/test_realtime_phone_gateway.py`

- [ ] **Step 1: Write failing payload test for upgraded exchange fields**

Add this test near the existing call-result or committed-history tests in `tests/test_realtime_phone_gateway.py`:

```python
def test_call_result_payload_uses_committed_exchanges_as_authoritative_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=10.0,
        last_seen_at=10.0,
        expected_frame_bytes=320,
    )
    session.disconnected_at = 12.0
    session.committed_exchanges.append(
        ConversationExchange(
            turn_id=1,
            status="interrupted",
            input_transcript="这个费用是什么？",
            output_transcript="这是三月份的物业费。",
            heard_output_transcript="这是三月份",
            played_audio_ms=820,
            playback_completed=False,
            source="client_interrupt",
            created_at_ms=1770000005000,
        )
    )

    payload = server._build_call_result_payload(session)

    assert "input_transcripts" not in payload
    assert "output_transcripts" not in payload
    assert payload["committed_exchanges"] == [
        {
            "turn_id": 1,
            "status": "interrupted",
            "question_id": None,
            "reply_id": None,
            "input_transcript": "这个费用是什么？",
            "output_transcript": "这是三月份的物业费。",
            "heard_output_transcript": "这是三月份",
            "played_audio_ms": 820,
            "playback_completed": False,
            "source": "client_interrupt",
            "created_at_ms": 1770000005000,
        }
    ]
    assert payload["metrics"]["gateway_history_interrupted_turns"] == 1
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_call_result_payload_uses_committed_exchanges_as_authoritative_history -q
```

Expected: fail because `ConversationExchange` does not accept the new fields and payload does not include them.

- [ ] **Step 3: Upgrade `ConversationExchange`**

Replace the existing `ConversationExchange` dataclass in `app/realtime_phone_gateway.py` with:

```python
@dataclass
class ConversationExchange:
    turn_id: int
    status: str = "completed"
    input_transcript: str = ""
    output_transcript: str = ""
    heard_output_transcript: str = ""
    question_id: str | None = None
    reply_id: str | None = None
    played_audio_ms: int = 0
    playback_completed: bool = False
    source: str = ""
    created_at_ms: int | None = None
```

- [ ] **Step 4: Add history metrics to session stats**

In `RealtimePhoneSessionStats`, add these counters near the existing gateway history counters:

```python
    gateway_history_completed_turns: int = 0
    gateway_history_interrupted_turns: int = 0
    gateway_history_missing_output_turns: int = 0
```

Keep `gateway_history_committed_turns` and `gateway_history_abandoned_turns` for backward-compatible metrics until callers are migrated.

- [ ] **Step 5: Expand `_build_call_result_payload()`**

Modify committed exchange serialization in `_build_call_result_payload()`:

```python
        committed_exchanges = [
            {
                "turn_id": exchange.turn_id,
                "status": exchange.status,
                "question_id": exchange.question_id,
                "reply_id": exchange.reply_id,
                "input_transcript": exchange.input_transcript,
                "output_transcript": exchange.output_transcript,
                "heard_output_transcript": exchange.heard_output_transcript,
                "played_audio_ms": exchange.played_audio_ms,
                "playback_completed": exchange.playback_completed,
                "source": exchange.source,
                "created_at_ms": exchange.created_at_ms,
            }
            for exchange in session.committed_exchanges
        ]
```

Do not return compatibility transcript arrays. `committed_exchanges` is the only
authoritative dialogue history in the call result payload. Downstream consumers
that need text-only lists can derive them from `committed_exchanges`.

Add metrics:

```python
                "gateway_history_completed_turns": (
                    session.gateway_history_completed_turns
                ),
                "gateway_history_interrupted_turns": (
                    session.gateway_history_interrupted_turns
                ),
                "gateway_history_missing_output_turns": (
                    session.gateway_history_missing_output_turns
                ),
```

- [ ] **Step 6: Run focused payload test**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_call_result_payload_uses_committed_exchanges_as_authoritative_history -q
```

Expected: pass.

## Task 4: Commit Interrupted Turns and Drop Late Old Audio

**Files:**
- Modify: `app/realtime_phone_gateway.py`
- Test: `tests/test_realtime_phone_gateway.py`

- [ ] **Step 1: Write failing unit test for interrupted turn ledger entry**

Add this test near the committed-history tests:

```python
def test_abandoned_pending_turn_is_committed_as_interrupted_history():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.pending_exchanges[3] = ConversationExchange(
        turn_id=3,
        input_transcript="这个费用是什么？",
        output_transcript="这是三月份的物业费。",
    )
    session.turn_first_playback_at[3] = 10.0
    session.turn_last_playback_at[3] = 10.82

    server._abandon_pending_turn(session, 3, reason="user_interrupt")

    assert session.pending_exchanges == {}
    assert len(session.committed_exchanges) == 1
    exchange = session.committed_exchanges[0]
    assert exchange.status == "interrupted"
    assert exchange.output_transcript == "这是三月份的物业费。"
    assert exchange.heard_output_transcript == ""
    assert exchange.playback_completed is False
    assert exchange.source == "client_interrupt"
    assert exchange.played_audio_ms == 820
    assert session.gateway_history_interrupted_turns == 1
```

- [ ] **Step 2: Write failing async test for late old audio dropping**

Add this test:

```python
def test_realtime_gateway_drops_late_audio_for_closed_interrupted_turn():
    asyncio.run(_assert_realtime_gateway_drops_late_audio_for_closed_interrupted_turn())


async def _assert_realtime_gateway_drops_late_audio_for_closed_interrupted_turn():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.closed_output_turn_ids.add(3)

    await server._queue_audio_delta(session, 3, samples_to_pcm_s16le([1000] * 480))

    assert session.playback_queue.empty()
    assert session.dropped_stale_frames == 1
```

- [ ] **Step 3: Run focused tests and verify they fail**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_abandoned_pending_turn_is_committed_as_interrupted_history tests/test_realtime_phone_gateway.py::test_realtime_gateway_drops_late_audio_for_closed_interrupted_turn -q
```

Expected: fail because interrupted turns are still abandoned and `closed_output_turn_ids` does not exist.

- [ ] **Step 4: Add closed turn tracking to session stats**

In `RealtimePhoneSessionStats`, add:

```python
    closed_output_turn_ids: set[int] = field(default_factory=set, repr=False)
```

- [ ] **Step 5: Add playback progress helper**

Add this helper near `_elapsed_ms()` helper usage or as a private method on `FreeSwitchRealtimeGatewayServer`:

```python
    @staticmethod
    def _played_audio_ms_for_turn(
        session: RealtimePhoneSessionStats,
        turn_id: int,
    ) -> int:
        first = session.turn_first_playback_at.get(turn_id)
        last = session.turn_last_playback_at.get(turn_id)
        if first is None or last is None or last < first:
            return 0
        return int((last - first) * 1000)
```

- [ ] **Step 6: Replace abandon behavior with interrupted commit**

Modify `_abandon_pending_turn()` so it commits an interrupted exchange instead of dropping it:

```python
    def _abandon_pending_turn(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int | None,
        *,
        reason: str,
    ) -> None:
        if turn_id is None:
            return
        session.closed_output_turn_ids.add(turn_id)
        exchange = session.pending_exchanges.pop(turn_id, None)
        output_text = session.output_transcripts_by_turn.get(turn_id, "")
        if exchange is None:
            exchange = ConversationExchange(turn_id=turn_id)
        if output_text and not exchange.output_transcript:
            exchange.output_transcript = output_text
        if not exchange.input_transcript and not exchange.output_transcript:
            session.gateway_history_abandoned_turns += 1
            return

        exchange.status = "interrupted"
        exchange.playback_completed = False
        exchange.played_audio_ms = self._played_audio_ms_for_turn(session, turn_id)
        exchange.heard_output_transcript = ""
        exchange.source = "client_interrupt"
        exchange.created_at_ms = int(time.time() * 1000)
        session.committed_exchanges.append(exchange)
        session.gateway_history_committed_turns += 1
        session.gateway_history_interrupted_turns += 1
        if not exchange.output_transcript:
            session.gateway_history_missing_output_turns += 1
```

Keep the existing logger, but update its event name to `gateway_conversation_turn_interrupted_committed` and include `played_audio_ms`.

- [ ] **Step 7: Drop late old audio**

At the top of `_queue_audio_delta()`, before checking `current_output_turn_id`, add:

```python
        if turn_id in session.closed_output_turn_ids:
            session.dropped_stale_frames += 1
            return
```

- [ ] **Step 8: Update completed commit metadata**

In `_commit_played_turn()`, before appending:

```python
        exchange.status = "completed"
        exchange.playback_completed = True
        exchange.played_audio_ms = self._played_audio_ms_for_turn(session, turn_id)
        exchange.heard_output_transcript = exchange.output_transcript
        exchange.source = "playback_completed"
        exchange.created_at_ms = int(time.time() * 1000)
```

Increment the new completed counter:

```python
        session.gateway_history_completed_turns += 1
```

- [ ] **Step 9: Run focused tests**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_abandoned_pending_turn_is_committed_as_interrupted_history tests/test_realtime_phone_gateway.py::test_realtime_gateway_drops_late_audio_for_closed_interrupted_turn tests/test_realtime_phone_gateway.py::test_realtime_phone_gateway_plays_model_audio_back_to_client -q
```

Expected: all tests pass.

## Task 5: Build `dialog_context` for Same-Process New Sessions

**Files:**
- Modify: `app/realtime_phone_gateway.py`
- Test: `tests/test_realtime_phone_gateway.py`

- [ ] **Step 1: Write failing test for dialog context from committed history**

Add this test near `test_realtime_dialog_config_anchors_postgres_employee_identity`:

```python
def test_realtime_dialog_config_uses_committed_history_as_dialog_context():
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
    )
    session = RealtimePhoneSessionStats(
        call_id="test-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
        prompt_snapshot=PromptSnapshot(
            scene="fee",
            version="v1",
            instructions="完整业务提示词",
            metadata={"employee_name": "小明"},
            content_hash="hash-a",
        ),
    )
    session.committed_exchanges.extend(
        [
            ConversationExchange(
                turn_id=1,
                status="completed",
                input_transcript="你是哪边？",
                output_transcript="我是物业中心小明。",
            ),
            ConversationExchange(
                turn_id=2,
                status="interrupted",
                input_transcript="这个费用是什么？",
                output_transcript="这是三月份的物业费。",
            ),
        ]
    )

    dialog_config = server._dialog_config_for_realtime_session(session)

    assert [item.role for item in dialog_config.dialog_context] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [item.text for item in dialog_config.dialog_context] == [
        "你是哪边？",
        "我是物业中心小明。",
        "这个费用是什么？",
        "这是三月份的物业费。",
    ]
```

- [ ] **Step 2: Run focused test and verify it fails**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_realtime_dialog_config_uses_committed_history_as_dialog_context -q
```

Expected: fail because `dialog_context` is empty.

- [ ] **Step 3: Import `RealtimeDialogContextItem`**

Modify the import from `app.realtime_types` in `app/realtime_phone_gateway.py`:

```python
from .realtime_types import (
    DEFAULT_INPUT_SAMPLE_RATE,
    DEFAULT_OUTPUT_SAMPLE_RATE,
    RealtimeDialogConfig,
    RealtimeDialogContextItem,
    RealtimeTurnResult,
)
```

- [ ] **Step 4: Add context builder helper**

Add this method to `FreeSwitchRealtimeGatewayServer` near `_instructions_for_realtime_session()`:

```python
    def _dialog_context_for_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> tuple[RealtimeDialogContextItem, ...]:
        items: list[RealtimeDialogContextItem] = []
        remaining_chars = MAX_COMMITTED_HISTORY_CHARS
        selected = session.committed_exchanges[-MAX_COMMITTED_HISTORY_EXCHANGES:]
        for exchange in selected:
            user_text = exchange.input_transcript.strip()
            assistant_text = exchange.output_transcript.strip()
            if not user_text or not assistant_text:
                continue
            block_len = len(user_text) + len(assistant_text)
            if block_len > remaining_chars:
                break
            items.append(RealtimeDialogContextItem(role="user", text=user_text))
            items.append(
                RealtimeDialogContextItem(role="assistant", text=assistant_text)
            )
            remaining_chars -= block_len
        return tuple(items)
```

- [ ] **Step 5: Attach context to dialog config**

In `_dialog_config_for_realtime_session()`, compute:

```python
        dialog_context = self._dialog_context_for_realtime_session(session)
```

Return it in all `RealtimeDialogConfig` branches:

```python
        if session.prompt_snapshot is None:
            return RealtimeDialogConfig(dialog_context=dialog_context)
```

```python
        if not employee_name:
            return RealtimeDialogConfig(dialog_context=dialog_context)
```

And in the business identity return:

```python
        return RealtimeDialogConfig(
            bot_name=_dialog_bot_name(employee_name),
            system_role=system_role,
            speaking_style=speaking_style,
            model=DEFAULT_DIALOG_MODEL,
            dialog_context=dialog_context,
        )
```

- [ ] **Step 6: Avoid duplicate history in system instructions**

Change `_instructions_for_realtime_session()` so the header no longer claims “电话用户已经完整听到的历史对话”. For first-stage implementation, keep latest-utterance guard and opening guard in prompt text, and move committed QA to `dialog_context`.

Replace the branch that appends committed history with:

```python
        return "\n".join(
            [instructions, "", LATEST_UTTERANCE_GUARD, *opening_lines]
        )
```

Then update tests that asserted committed history appears in instructions. The new assertion should check committed history appears in `dialog_config.dialog_context`, not in the system prompt.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_realtime_dialog_config_uses_committed_history_as_dialog_context tests/test_realtime_phone_gateway.py::test_realtime_instructions_guard_latest_utterance_against_history -q
```

Expected: both tests pass after updating the old assertion to the new dialog_context behavior.

## Task 6: Update Interruption and Hot-Restart Tests to Match First-Stage Scope

**Files:**
- Modify: `tests/test_realtime_phone_gateway.py`

- [ ] **Step 1: Update tests that expect repair audio replay on normal interruption**

These existing tests reflect the old hot-restart/repair path and must be updated or replaced:

```python
test_realtime_phone_gateway_replays_interrupt_audio_after_hot_restart
test_realtime_phone_gateway_replays_interrupt_audio_when_context_repair_fails
test_realtime_phone_gateway_waits_for_slow_context_repair_before_replay
```

Keep coverage for fallback restart behavior if `restart_on_interruption=True`, but add a first-stage normal path test where `restart_on_interruption=False` asserts:

```python
assert fake_session.interruption_calls == ["hello from model"]
assert fake_session.close_calls == 1
assert fake_session.connect_calls == 1
assert fake_session.append_calls >= 4
assert stats.realtime_session_restarts == 0
```

Use the existing async helper pattern in `tests/test_realtime_phone_gateway.py`.

- [ ] **Step 2: Add assertion that interrupted exchange is committed in integration interrupt test**

In `_assert_realtime_phone_gateway_interrupts_playback()`, after the current stats assertions, add:

```python
    interrupted = [
        exchange
        for exchange in stats.committed_exchanges
        if exchange.status == "interrupted"
    ]
    assert interrupted
    assert interrupted[0].output_transcript == "hello from model"
    assert interrupted[0].playback_completed is False
```

- [ ] **Step 3: Run realtime gateway tests**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py -q
```

Expected: all tests pass.

## Task 7: Full Verification and Documentation Sync Check

**Files:**
- Modify only if tests reveal a mismatch: `docs/realtime-context-management-commercial-design.md`

- [ ] **Step 1: Run the agreed gateway test set**

Run:

```bash
uv run --with pytest pytest tests/test_doubao_s2s_realtime.py tests/test_realtime_phone_gateway.py tests/test_doubao_s2s_client.py -q
```

Expected: the command exits with code 0 and all collected tests pass. Any failure must be fixed before completion.

- [ ] **Step 2: Run keyword guard against accidental main-path additions**

Run:

```bash
rg -n "ConversationRetrieve|ConversationCreate|ConversationTruncate|enable_conversation_truncate" app tests
```

Expected: no production main-path use of `ConversationRetrieve`, `ConversationCreate`, `ConversationTruncate`, or `enable_conversation_truncate`. Test references are acceptable only when they explicitly describe optional or future behavior.

- [ ] **Step 3: Inspect changed files**

Run:

```bash
git diff -- app/realtime_types.py app/doubao_s2s_client.py app/doubao_s2s_realtime.py app/realtime_phone_gateway.py tests/test_doubao_s2s_client.py tests/test_doubao_s2s_realtime.py tests/test_realtime_phone_gateway.py docs/realtime-context-management-commercial-design.md
```

Expected: diff contains only first-stage context-management changes.

- [ ] **Step 4: Final implementation summary**

Summarize:

- Normal interruption no longer hot-restarts provider session.
- Interrupted turns enter `committed_exchanges(status=interrupted)` with full `output_transcript`.
- Late old audio for closed turns is dropped by `turn_id`.
- Same-process new sessions receive recent committed QA through `dialog_context`.
- `input_transcripts/output_transcripts` are not emitted; downstream consumers should derive text-only lists from `committed_exchanges` if needed.

## Self-Review

Spec coverage:

- Full interrupted replies in `committed_exchanges`: Task 3 and Task 4.
- Normal interruption without hot restart: Task 2 and Task 6.
- Late old audio safety: Task 4.
- Same-process hot restart/new session `dialog_context`: Task 1 and Task 5.
- No Retrieve/Create/Truncate in first-stage main path: Task 2 and Task 7.
- Playback fact fields and payload compatibility: Task 3 and Task 4.

Placeholder scan:

- The plan contains concrete file paths, test names, commands, and code snippets.
- The plan intentionally does not include `ConversationRetrieve`, `ConversationCreate`, or `ConversationTruncate` implementation tasks.

Type consistency:

- `RealtimeDialogContextItem` is defined in `app/realtime_types.py` and consumed by `app/doubao_s2s_client.py` and `app/realtime_phone_gateway.py`.
- `ConversationExchange.status` values are `completed` and `interrupted`.
- `source` values used in snippets are `playback_completed` and `client_interrupt`; `restart_rebuild` is reserved for future fallback reconstruction.
