# 坐席页人工接管建议实施计划

> **给 Agent 实施者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务执行。本计划使用 checkbox（`- [ ]`）跟踪进度。

**目标：** 实现第一版“客户说我想投诉时，在 WebRTC 坐席页提示建议人工接管；坐席点击后才打断 AI 并进入现有人工接管链路”。

**架构：** 在现有 `handoff` 状态旁边新增轻量运行态 `agent_takeover_suggestion`，只表示“建议坐席关注并可主动接管”。实时媒体网关识别归一化后等于 `我想投诉` 的 ASR 文本后，调用 `OutboundCallManager` 记录建议，但不调用现有 `request_handoff`，不停止 AI 播放。坐席页轮询 active calls，展示建议卡片，坐席点击后再升级为现有 `request_handoff -> claim_handoff -> uuid_bridge` 流程。

**技术栈：** Python dataclass、现有 `OutboundCallManager`、现有 `FreeSwitchRealtimeGatewayServer`、标准库 HTTP server、`static/webrtc-agent-test.html` 原生 JavaScript、pytest。

---

## 文件结构

- 修改 `app/call_control.py`：新增 `AgentTakeoverSuggestion`，挂到 `OutboundCallRecord`，通过 `to_dict()` 输出，新增 `record_agent_takeover_suggestion()`，并计算 `can_takeover`。
- 修改 `app/realtime_phone_gateway.py`：新增投诉接管建议识别和回调；保证建议识别不会停止 AI、不设置 `handoff_requested`。
- 修改 `app/main.py`：把 `outbound_manager.record_agent_takeover_suggestion` 注入实时媒体网关。
- 修改 `static/webrtc-agent-test.html`：展示建议接管列表，点击“人工接管”后先创建正式 handoff，再复用现有 claim。
- 修改 `tests/test_call_control.py`：覆盖运行态序列化、终态 `can_takeover=false`、正式 handoff 存在时不允许建议接管。
- 修改 `tests/test_realtime_phone_gateway.py`：覆盖“我想投诉”精确识别，以及“记录建议但不触发 handoff”。

## 任务 1：新增后端运行态建议状态

**文件：**

- 修改：`app/call_control.py`
- 测试：`tests/test_call_control.py`

- [ ] **步骤 1：先写失败测试，覆盖建议状态序列化**

在 `tests/test_call_control.py` 现有 handoff 测试附近加入：

```python
def test_outbound_manager_records_agent_takeover_suggestion_without_handoff():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call-1"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(
            event_socket=EventSocketConfig(enabled=True),
            outbound=OutboundCallConfig(endpoint_template="user/{destination}"),
        ),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        active_call = _wait_for_status(manager, call["call_id"], "originated")

        suggested_call = manager.record_agent_takeover_suggestion(
            active_call["call_id"],
            {
                "reason": "complaint",
                "last_utterance": "我想投诉",
            },
        )

        assert suggested_call["status"] == "originated"
        assert suggested_call["handoff"] is None
        assert suggested_call["agent_takeover_suggestion"] == {
            "state": "suggested",
            "reason": "complaint",
            "last_utterance": "我想投诉",
            "suggested_at_ms": suggested_call["agent_takeover_suggestion"]["suggested_at_ms"],
            "updated_at_ms": suggested_call["agent_takeover_suggestion"]["updated_at_ms"],
            "can_takeover": True,
        }
    finally:
        manager.shutdown()


def test_outbound_manager_disables_takeover_suggestion_after_terminal_status():
    class FakeDialer:
        async def resolve_endpoint(self, endpoint: str) -> str:
            return endpoint

        async def originate(self, command: str) -> str:
            return "+OK call-1"

        async def hangup(self, call_id: str, *, cause: str) -> str:
            return "+OK hangup accepted"

    manager = OutboundCallManager(
        GatewayConfig(outbound=OutboundCallConfig(endpoint_template="user/{destination}")),
        dialer_factory=lambda: FakeDialer(),
    )

    try:
        call = manager.create_call({"destination": "1000"})
        active_call = _wait_for_status(manager, call["call_id"], "originated")
        manager.record_agent_takeover_suggestion(
            active_call["call_id"],
            {"reason": "complaint", "last_utterance": "我想投诉"},
        )

        manager.handle_channel_event(
            ChannelStateEvent(
                name="CHANNEL_HANGUP_COMPLETE",
                call_id=active_call["call_id"],
                hangup_cause="NORMAL_CLEARING",
            )
        )

        final_call = manager.get_call(active_call["call_id"])
        assert final_call is not None
        assert final_call["agent_takeover_suggestion"]["can_takeover"] is False
    finally:
        manager.shutdown()
```

- [ ] **步骤 2：运行测试，确认失败**

运行：

```bash
uv run --with pytest pytest tests/test_call_control.py::test_outbound_manager_records_agent_takeover_suggestion_without_handoff tests/test_call_control.py::test_outbound_manager_disables_takeover_suggestion_after_terminal_status -q
```

预期：失败，原因是 `record_agent_takeover_suggestion` 和 `agent_takeover_suggestion` 还不存在。

- [ ] **步骤 3：实现最小后端运行态**

在 `app/call_control.py` 的 `HandoffState` 后新增：

```python
@dataclass
class AgentTakeoverSuggestion:
    state: str
    suggested_at_ms: int
    updated_at_ms: int
    reason: str | None = None
    last_utterance: str | None = None

    def to_dict(self, *, can_takeover: bool) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "last_utterance": self.last_utterance,
            "suggested_at_ms": self.suggested_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "can_takeover": can_takeover,
        }
```

在 `OutboundCallRecord` 增加字段：

```python
    agent_takeover_suggestion: AgentTakeoverSuggestion | None = None
```

在 `OutboundCallRecord.to_dict()` 中计算输出：

```python
        takeover_suggestion_payload = (
            None
            if self.agent_takeover_suggestion is None
            else self.agent_takeover_suggestion.to_dict(
                can_takeover=_can_takeover_from_suggestion(self)
            )
        )
```

在返回字典中靠近 `handoff` 的位置加入：

```python
            "agent_takeover_suggestion": takeover_suggestion_payload,
```

在 `_is_handoff_inactive_status` 附近新增：

```python
def _can_takeover_from_suggestion(record: OutboundCallRecord) -> bool:
    if record.agent_takeover_suggestion is None:
        return False
    if record.handoff is not None:
        return False
    return not _is_handoff_inactive_status(record.status)
```

在 `OutboundCallManager` 靠近 `request_handoff()` 的位置新增：

```python
    def record_agent_takeover_suggestion(
        self,
        call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        _require_safe_token(call_id, "call_id")
        reason = _optional_str(payload, "reason") or "complaint"
        last_utterance = _optional_str(payload, "last_utterance")
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise CallControlError("call not found", status_code=404)
            if _is_handoff_inactive_status(record.status):
                raise CallControlError("call is not active", status_code=409)
            now_ms = _now_ms()
            if record.agent_takeover_suggestion is None:
                record.agent_takeover_suggestion = AgentTakeoverSuggestion(
                    state="suggested",
                    reason=reason,
                    last_utterance=last_utterance,
                    suggested_at_ms=now_ms,
                    updated_at_ms=now_ms,
                )
            else:
                record.agent_takeover_suggestion.state = "suggested"
                record.agent_takeover_suggestion.reason = reason
                record.agent_takeover_suggestion.last_utterance = last_utterance
                record.agent_takeover_suggestion.updated_at_ms = now_ms
            record.updated_at_ms = now_ms
            return record.to_dict()
```

- [ ] **步骤 4：运行聚焦测试**

运行：

```bash
uv run --with pytest pytest tests/test_call_control.py::test_outbound_manager_records_agent_takeover_suggestion_without_handoff tests/test_call_control.py::test_outbound_manager_disables_takeover_suggestion_after_terminal_status -q
```

预期：通过。

## 任务 2：实时网关识别投诉建议但不触发 handoff

**文件：**

- 修改：`app/realtime_phone_gateway.py`
- 修改：`app/main.py`
- 测试：`tests/test_realtime_phone_gateway.py`

- [ ] **步骤 1：先写失败测试，覆盖精确识别和不触发 handoff**

更新 `tests/test_realtime_phone_gateway.py` 的 import，加入 `_detect_agent_takeover_suggestion`。

在 `test_handoff_request_detection_is_conservative()` 附近新增：

```python
def test_agent_takeover_suggestion_detection_is_exact_for_first_version():
    assert _detect_agent_takeover_suggestion("我想投诉") == "complaint"
    assert _detect_agent_takeover_suggestion("我想，投诉") == "complaint"
    assert _detect_agent_takeover_suggestion(" 我 想 投 诉 ") == "complaint"
    assert _detect_agent_takeover_suggestion("我想投诉你们物业") is None
    assert _detect_agent_takeover_suggestion("我不是想投诉") is None
    assert _detect_agent_takeover_suggestion("我要转人工") is None
```

在 `FakeHandoffRequester` 附近新增：

```python
class FakeAgentTakeoverSuggestionRecorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, call_id: str, payload: dict) -> dict:
        self.requests.append((call_id, dict(payload)))
        return {
            "status": "accepted",
            "call": {
                "agent_takeover_suggestion": {
                    "state": "suggested",
                    "reason": payload.get("reason"),
                    "last_utterance": payload.get("last_utterance"),
                    "can_takeover": True,
                }
            },
        }
```

在现有 handoff helper 附近新增：

```python
async def _assert_realtime_gateway_records_takeover_suggestion_without_handoff():
    fake_handoff = FakeHandoffRequester()
    fake_suggestion = FakeAgentTakeoverSuggestionRecorder()
    fake_playback_control = FakePlaybackControl()
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        playback_control=fake_playback_control,
        handoff_requester=fake_handoff,
        agent_takeover_suggestion_recorder=fake_suggestion,
    )
    session = RealtimePhoneSessionStats(
        call_id="customer-call",
        session_id="test-session",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )
    session.current_output_turn_id = 3

    await server._handle_input_transcript_available(session, 4, "我想投诉")

    assert fake_suggestion.requests == [
        (
            "customer-call",
            {
                "reason": "complaint",
                "last_utterance": "我想投诉",
            },
        )
    ]
    assert fake_handoff.requests == []
    assert session.handoff_requested is False
    assert session.agent_takeover_suggestion_requested is True
    assert session.current_output_turn_id == 3
    assert fake_playback_control.break_calls == []
```

新增公开测试：

```python
def test_realtime_gateway_records_takeover_suggestion_without_handoff():
    asyncio.run(_assert_realtime_gateway_records_takeover_suggestion_without_handoff())
```

- [ ] **步骤 2：运行测试，确认失败**

运行：

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_agent_takeover_suggestion_detection_is_exact_for_first_version tests/test_realtime_phone_gateway.py::test_realtime_gateway_records_takeover_suggestion_without_handoff -q
```

预期：失败，原因是 `_detect_agent_takeover_suggestion`、session 字段和构造参数尚不存在。

- [ ] **步骤 3：实现实时网关识别和回调**

在 `app/realtime_phone_gateway.py` 的 `HandoffRequester` 附近新增：

```python
AgentTakeoverSuggestionRecorder = Callable[[str, dict[str, Any]], Mapping[str, Any]]
```

在 `RealtimePhoneSessionStats` 增加：

```python
    agent_takeover_suggestion_requested: bool = False
    agent_takeover_suggestion_result: dict[str, Any] | None = field(default=None, repr=False)
    agent_takeover_suggestion_error: str | None = None
```

给 `FreeSwitchRealtimeGatewayServer.__init__` 增加参数：

```python
        agent_takeover_suggestion_recorder: (
            AgentTakeoverSuggestionRecorder | None
        ) = None,
```

在构造函数中保存：

```python
        self._agent_takeover_suggestion_recorder = agent_takeover_suggestion_recorder
```

在 `_handle_input_transcript_available()` 中，拿到 `normalized` 后先调用：

```python
        await self._maybe_record_agent_takeover_suggestion(
            session,
            normalized,
        )
```

在 `_trigger_handoff_from_turn()` 附近新增：

```python
    async def _maybe_record_agent_takeover_suggestion(
        self,
        session: RealtimePhoneSessionStats,
        transcript: str,
    ) -> None:
        if session.agent_takeover_suggestion_requested:
            return
        recorder = self._agent_takeover_suggestion_recorder
        if recorder is None:
            return
        reason = _detect_agent_takeover_suggestion(transcript)
        if reason is None:
            return
        payload = {
            "reason": reason,
            "last_utterance": transcript.strip(),
        }
        try:
            result = await asyncio.to_thread(recorder, session.call_id, payload)
        except Exception as err:
            session.agent_takeover_suggestion_error = str(err)
            LOGGER.warning(
                "realtime_phone_takeover_suggestion_failed call_id=%s "
                "session_id=%s reason=%s error=%s",
                session.call_id,
                session.session_id,
                reason,
                err,
                exc_info=True,
            )
            return
        session.agent_takeover_suggestion_requested = True
        session.agent_takeover_suggestion_result = dict(result)
        LOGGER.info(
            "realtime_phone_takeover_suggestion_recorded call_id=%s "
            "session_id=%s reason=%s input_transcript=%s",
            session.call_id,
            session.session_id,
            reason,
            transcript,
        )
```

在 `_detect_handoff_request()` 附近新增：

```python
def _detect_agent_takeover_suggestion(text: str) -> str | None:
    normalized = re.sub(r"[\s，。！？、,.!?；;：:]+", "", text or "")
    if normalized == "我想投诉":
        return "complaint"
    return None
```

在 `_finalize_server_vad_turn()` 中，处理完 `handoff_reason` 后、正常输出处理前调用：

```python
        await self._maybe_record_agent_takeover_suggestion(
            session,
            result.input_transcript,
        )
```

在 `app/main.py` 创建 `FreeSwitchRealtimeGatewayServer` 时增加：

```python
            agent_takeover_suggestion_recorder=(
                outbound_manager.record_agent_takeover_suggestion
            ),
```

- [ ] **步骤 4：运行聚焦测试**

运行：

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_agent_takeover_suggestion_detection_is_exact_for_first_version tests/test_realtime_phone_gateway.py::test_realtime_gateway_records_takeover_suggestion_without_handoff -q
```

预期：通过。

## 任务 3：在 WebRTC 坐席页展示接管建议

**文件：**

- 修改：`static/webrtc-agent-test.html`

- [ ] **步骤 1：新增页面 DOM 和状态变量**

在 `static/webrtc-agent-test.html` 的“待接通话”区域中，现有等待人工列表后、AI 对话记录列前插入：

```html
<div>
  <label>建议人工接管</label>
  <div id="takeoverSuggestionList" class="handoff-list">
    <div class="empty">暂无接管建议</div>
  </div>
</div>
```

在 `els` 中新增：

```javascript
takeoverSuggestionList: document.getElementById("takeoverSuggestionList"),
```

在状态变量区新增：

```javascript
let takeoverSuggestionCalls = [];
let selectedTakeoverSuggestionCall = null;
```

- [ ] **步骤 2：拆出通用 claim 逻辑**

把 `claimSelectedHandoff()` 替换为：

```javascript
async function claimSelectedHandoff() {
  if (!selectedHandoffCall) return;
  await claimHandoffCall(selectedHandoffCall.call_id);
}
```

在其下方新增：

```javascript
async function claimHandoffCall(callId) {
  claimInFlight = true;
  setControls();
  try {
    log("提交坐席 claim", { call_id: callId });
    const response = await fetch(`/calls/${encodeURIComponent(callId)}/handoff/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_extension: currentAgentExtension(),
        claimed_by: currentAgentExtension(),
        timeout_seconds: 20,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    selectedHandoffCall = payload.call;
    selectedTakeoverSuggestionCall = null;
    renderHandoffTimeline(selectedHandoffCall);
    log("坐席 claim 已完成，等待浏览器来电并点击接听", payload.call.handoff);
    await refreshHandoffCalls({ preserveSelected: true });
  } catch (error) {
    log("坐席 claim 失败", { call_id: callId, error: error.message });
  } finally {
    claimInFlight = false;
    setControls();
  }
}
```

- [ ] **步骤 3：从 `/calls` 轮询结果渲染建议卡片**

在 `refreshHandoffCalls()` 中，`waitingHandoffCalls = ...` 后加入：

```javascript
takeoverSuggestionCalls = (payload.calls || []).filter((call) => {
  const suggestion = call.agent_takeover_suggestion;
  return (
    suggestion &&
    suggestion.state === "suggested" &&
    suggestion.can_takeover &&
    !(call.handoff && call.handoff.state)
  );
});
```

在 `renderHandoffList({ preserveSelected });` 后调用：

```javascript
renderTakeoverSuggestionList();
```

在 `renderHandoffList()` 附近新增：

```javascript
function renderTakeoverSuggestionList() {
  if (!takeoverSuggestionCalls.length) {
    els.takeoverSuggestionList.innerHTML = '<div class="empty">暂无接管建议</div>';
    selectedTakeoverSuggestionCall = null;
    setControls();
    return;
  }
  els.takeoverSuggestionList.innerHTML = "";
  takeoverSuggestionCalls.forEach((call) => {
    const suggestion = call.agent_takeover_suggestion || {};
    const button = document.createElement("button");
    button.type = "button";
    button.className = "handoff-item";
    button.innerHTML = `
      <strong>${escapeHtml(call.call_id)}</strong>
      <span class="muted">客户原话：${escapeHtml(suggestion.last_utterance || "-")}</span>
      <span class="muted">原因：客户投诉</span>
      <span class="muted">点击后将打断 AI 并接管</span>
    `;
    button.addEventListener("click", () => takeOverSuggestedCall(call));
    els.takeoverSuggestionList.appendChild(button);
  });
  setControls();
}
```

- [ ] **步骤 4：实现点击建议后升级为正式转人工**

在 `claimSelectedHandoff()` 附近新增：

```javascript
async function takeOverSuggestedCall(call) {
  const callId = call && call.call_id;
  if (!callId) return;
  if (!ua || !ua.isRegistered()) {
    log("无法接管建议通话：坐席未上线", { call_id: callId });
    return;
  }
  if (currentSession || pendingIncomingSession) {
    log("无法接管建议通话：当前坐席已有通话", { call_id: callId });
    return;
  }
  claimInFlight = true;
  setControls();
  const suggestion = call.agent_takeover_suggestion || {};
  try {
    log("将接管建议升级为正式转人工", { call_id: callId });
    const response = await fetch(`/calls/${encodeURIComponent(callId)}/handoff`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        trigger: "agent_takeover_suggestion",
        reason: suggestion.reason || "complaint",
        last_utterance: suggestion.last_utterance || "我想投诉",
        wait_timeout_seconds: 180,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    selectedHandoffCall = payload.call;
    selectedTakeoverSuggestionCall = null;
    renderHandoffTimeline(selectedHandoffCall);
    await claimHandoffCall(callId);
  } catch (error) {
    log("人工接管建议失败", { call_id: callId, error: error.message });
  } finally {
    claimInFlight = false;
    setControls();
  }
}
```

- [ ] **步骤 5：手工验证坐席页**

启动 realtime 模式，打开 `/webrtc-agent-test`。

预期：

```text
1. 无建议时显示“暂无接管建议”。
2. /calls 中有 active call 且 agent_takeover_suggestion.can_takeover=true 时，页面出现建议卡片。
3. 未注册坐席时点击建议卡片，日志显示“坐席未上线”。
4. 已注册坐席点击建议卡片后，依次调用 POST /calls/{call_id}/handoff 和 POST /calls/{call_id}/handoff/claim。
5. 原有 waiting_agent 待接通话列表仍可正常使用。
```

## 任务 4：集成验证

**文件：**

- 验证：`app/call_control.py`
- 验证：`app/realtime_phone_gateway.py`
- 验证：`app/main.py`
- 验证：`static/webrtc-agent-test.html`
- 测试：`tests/test_call_control.py`
- 测试：`tests/test_realtime_phone_gateway.py`

- [ ] **步骤 1：运行后端聚焦测试**

运行：

```bash
uv run --with pytest pytest tests/test_call_control.py tests/test_realtime_phone_gateway.py -q
```

预期：通过。

- [ ] **步骤 2：运行网关相关完整测试**

运行：

```bash
uv run --with pytest pytest -q
```

预期：通过。

- [ ] **步骤 3：检查编辑文件的诊断**

用 IDE diagnostics 检查：

```text
app/call_control.py
app/realtime_phone_gateway.py
app/main.py
static/webrtc-agent-test.html
tests/test_call_control.py
tests/test_realtime_phone_gateway.py
```

预期：没有新增诊断。

- [ ] **步骤 4：本地端到端冒烟验证**

启动 realtime 模式并发起本地测试通话。通话中说：

```text
我想投诉
```

预期：

```text
1. AI 继续通话，不因建议状态被打断。
2. /calls 返回 agent_takeover_suggestion，reason=complaint。
3. /webrtc-agent-test 展示“建议人工接管”。
4. 坐席点击人工接管。
5. 后端日志出现正式 handoff 和 claim。
6. FreeSWITCH bridge 客户通道和坐席通道。
```

不要创建 git commit，除非用户明确要求。
