# Browser Realtime Business Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a browser-only realtime business simulation page where a user can combine database `strategy_core`, `speaking_style`, `opening_template`, debt context, and editable public constraints before starting a session, then validate an experience close to the softphone path through the existing media WebSocket without affecting real outbound calls.

**Architecture:** Keep production public constraints in code and database business strategy unchanged. Add a browser business test session store keyed by `browser-` call IDs, expose a session registration endpoint that can either load database business strategy or accept manual inputs, and compose that store ahead of the existing outbound prompt snapshot provider when realtime media sessions start. The registration endpoint also prepares browser-only opening audio through the existing `OpeningAudioStore` when enabled; the browser page registers a session before connecting to `ws://{host}:9101/media/{call_id}` and must reconnect with a new call ID to test modified constraints.

**Tech Stack:** Python `http.server`, existing `websockets` media server, existing `PromptSnapshot` / `RealtimeDialogConfig`, plain HTML/CSS/JavaScript, Web Audio / AudioWorklet, pytest.

---

## File Structure

- Create: `app/browser_prompt_test.py`
  - Owns browser-only session registration, database/manual prompt composition, validation, prompt rendering, TTL cleanup, and lookup by `call_id`.
  - Depends on `app.postgres.PromptSnapshot` for the existing prompt snapshot shape.
  - Uses `PostgresPromptStore.prepare_business_prompt()` in database mode.
  - Uses `OpeningAudioStore` and existing opening generation code in database mode when opening playback is enabled.

- Modify: `app/health_server.py`
  - Serve `GET /browser-realtime-test`.
  - Accept `POST /browser-test-prompts` as the browser test session registration endpoint.
  - Pass requests to `BrowserPromptTestStore`.

- Modify: `app/main.py`
  - Instantiate one `BrowserPromptTestStore`.
  - Pass it to `HealthServer`.
  - Pass the existing `PostgresPromptStore`, `OpeningAudioStore`, and opening generator to the browser test store.
  - Compose `prompt_snapshot_provider`: browser prompt store first, `OutboundCallManager.get_prompt_snapshot` second.

- Create: `static/browser-realtime-test.html`
  - Browser dialogue test UI.
  - Browser business simulation form for `callId`, `debtId`, `identityName`, `personaId`, opening toggle, and public constraint overrides.
  - Can preview/edit database-loaded strategy, speaking style, and public constraints before registering the session.
  - Registers the browser business test session before opening media WebSocket.
  - Captures microphone, sends 8kHz s16le mono 20ms frames, plays returned PCM frames.

- Modify: `static/outbound-test.html`
  - Add navigation link to `/browser-realtime-test`.

- Modify: `app/health_server.py` document shell navigation
  - Add `/browser-realtime-test` to shared docs shell navigation.

- Create: `tests/test_browser_prompt_test.py`
  - Unit tests for manual prompt rendering, database prompt composition, opening metadata handling, call ID validation, TTL expiration, and store lookup.

- Modify: `tests/test_health_server.py`
  - HTTP tests for browser page and prompt registration endpoint.
  - Navigation assertions if existing page tests expect exact link counts.

- Modify: `tests/test_realtime_phone_gateway.py`
  - Cover that `prompt_snapshot_provider(call_id)` already takes precedence over fallback prompt behavior for browser-style call IDs if existing coverage is insufficient.

- Modify: `docs/browser-realtime-gateway-test-technical-design.md`
  - Keep the design document aligned with actual endpoint names and behavior after implementation.

---

## Final Scope

This plan targets the final browser effect:

```text
浏览器页面
  -> 输入 callId / debtId / identityName / personaId
  -> 从数据库读取 strategy_core / speaking_style / opening_template / 债务上下文 / 历史摘要
  -> 页面临时编辑公共约束分块和 speaking_style 覆盖项
  -> 创建 browser-{...} 测试会话
  -> 后端生成本次 PromptSnapshot
  -> 后端按需生成并缓存开场白音频
  -> 浏览器连接 ws://127.0.0.1:9101/media/{call_id}
  -> 网关先播放开场白，再进入实时对话
```

Still out of scope:

```text
SIP/RTP/FreeSWITCH/运营商验证
把浏览器测试规则写回数据库
对话中热更新当前豆包 session 的 prompt
伪造 /calls 外呼记录
```

---

## Task 1: Browser Prompt Store

**Files:**
- Create: `app/browser_prompt_test.py`
- Test: `tests/test_browser_prompt_test.py`

- [ ] **Step 1: Write failing tests for prompt rendering and metadata**

Create `tests/test_browser_prompt_test.py`:

```python
from __future__ import annotations

from app.browser_prompt_test import BrowserPromptTestStore, build_browser_prompt_snapshot


def test_build_browser_prompt_snapshot_renders_editable_public_constraints():
    snapshot = build_browser_prompt_snapshot(
        {
            "call_id": "browser-20260527-143000-a1b2",
            "employee_name": "测试员工",
            "identityName": "项目员工",
            "speaking_style": "测试客服口吻。",
            "sections": {
                "rule_priority": ["测试规则优先级。"],
                "critical_runtime": ["测试高优先级红线。"],
                "dialog_style": ["测试对话风格。"],
                "fact_boundary": ["测试事实边界。"],
                "privacy_disclosure": ["测试隐私边界。"],
                "amount_dispute": ["测试金额争议。"],
                "property_fee_scene": ["测试物业费场景。"],
                "extra": ["测试补充规则。"],
            },
        }
    )

    assert snapshot.scene == "browser-realtime-test"
    assert snapshot.version == "browser-test"
    assert snapshot.metadata["source"] == "browser-realtime-test"
    assert snapshot.metadata["employee_name"] == "测试员工"
    assert snapshot.metadata["identityName"] == "项目员工"
    assert snapshot.metadata["speaking_style"] == "测试客服口吻。"
    assert "# 角色" in snapshot.instructions
    assert "你是测试员工" in snapshot.instructions
    assert "# 规则优先级" in snapshot.instructions
    assert "测试规则优先级。" in snapshot.instructions
    assert "# 高优先级运行红线" in snapshot.instructions
    assert "测试高优先级红线。" in snapshot.instructions
    assert "# 浏览器测试补充规则" in snapshot.instructions
    assert "测试补充规则。" in snapshot.instructions
```

- [ ] **Step 2: Run the rendering test and verify RED**

Run:

```bash
uv run --with pytest pytest tests/test_browser_prompt_test.py::test_build_browser_prompt_snapshot_renders_editable_public_constraints -q
```

Expected: fail with `ModuleNotFoundError: No module named 'app.browser_prompt_test'`.

- [ ] **Step 3: Write failing tests for browser-only call IDs and TTL**

Append to `tests/test_browser_prompt_test.py`:

```python
def test_browser_prompt_store_only_accepts_browser_call_ids():
    store = BrowserPromptTestStore(ttl_seconds=1800, now=lambda: 100.0)

    snapshot = store.register(
        {
            "call_id": "browser-20260527-143000-a1b2",
            "sections": {"extra": ["本轮测试。"]},
        }
    )

    assert store.get("browser-20260527-143000-a1b2") is snapshot
    assert store.get("not-browser-call") is None

    try:
        store.register({"call_id": "real-call-id", "sections": {"extra": ["bad"]}})
    except ValueError as err:
        assert "browser-" in str(err)
    else:
        raise AssertionError("expected non-browser call id to be rejected")


def test_browser_prompt_store_expires_registered_snapshots():
    current_time = 100.0

    def now() -> float:
        return current_time

    store = BrowserPromptTestStore(ttl_seconds=10, now=now)
    store.register(
        {
            "call_id": "browser-expiring",
            "sections": {"extra": ["短期有效。"]},
        }
    )

    assert store.get("browser-expiring") is not None
    current_time = 111.0
    assert store.get("browser-expiring") is None
```

- [ ] **Step 4: Run store tests and verify RED**

Run:

```bash
uv run --with pytest pytest tests/test_browser_prompt_test.py -q
```

Expected: fail because `app.browser_prompt_test` does not exist.

- [ ] **Step 5: Implement `app/browser_prompt_test.py`**

Create `app/browser_prompt_test.py`:

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from .postgres import PromptSnapshot

BROWSER_PROMPT_SCENE = "browser-realtime-test"
BROWSER_PROMPT_VERSION = "browser-test"
BROWSER_CALL_ID_PREFIX = "browser-"
DEFAULT_BROWSER_PROMPT_TTL_SECONDS = 1800

SECTION_TITLES = {
    "rule_priority": "# 规则优先级",
    "critical_runtime": "# 高优先级运行红线",
    "dialog_style": "# 对话风格",
    "fact_boundary": "# 事实边界",
    "privacy_disclosure": "# 身份核实与隐私边界",
    "amount_dispute": "# 金额与争议处理",
    "property_fee_scene": "# 物业费场景约束",
    "extra": "# 浏览器测试补充规则",
}

DEFAULT_SECTION_VALUES = {
    "rule_priority": ["数据库业务策略不得突破身份核实、隐私保护、勿扰终止、支付安全、事实边界和法律红线。"],
    "critical_runtime": ["要求勿扰、拒绝继续沟通或明确要求不再联系时，必须礼貌结束。"],
    "dialog_style": ["电话回复保持简短自然，每次最多两句，先承接用户最新一句，再推进当前费用事项。"],
    "fact_boundary": ["不得编造系统未提供的金额、地址、缴费渠道、发票规则、维修进度或司法结果。"],
    "privacy_disclosure": ["未确认本人或授权处理人前，不得披露待处理金额、地址、房号、欠费明细或费用原因。"],
    "amount_dispute": ["金额、减免、分期和部分缴纳只按本轮明确策略说明，未授权时只能记录意向并提示以物业核实为准。"],
    "property_fee_scene": ["用户提出物业服务投诉时，先承接并记录诉求，不在同一回复继续催缴。"],
    "extra": [],
}


@dataclass(frozen=True)
class StoredBrowserPrompt:
    snapshot: PromptSnapshot
    expires_at: float


class BrowserPromptTestStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_BROWSER_PROMPT_TTL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._now = now or time.time
        self._items: dict[str, StoredBrowserPrompt] = {}

    def register(self, payload: dict[str, Any]) -> PromptSnapshot:
        call_id = _required_text(payload.get("call_id"), "call_id")
        _validate_browser_call_id(call_id)
        snapshot = build_browser_prompt_snapshot(payload)
        self._items[call_id] = StoredBrowserPrompt(
            snapshot=snapshot,
            expires_at=self._now() + self.ttl_seconds,
        )
        self._purge_expired()
        return snapshot

    def get(self, call_id: str) -> PromptSnapshot | None:
        stored = self._items.get(call_id)
        if stored is None:
            return None
        if stored.expires_at <= self._now():
            self._items.pop(call_id, None)
            return None
        return stored.snapshot

    def _purge_expired(self) -> None:
        now = self._now()
        expired = [
            call_id
            for call_id, stored in self._items.items()
            if stored.expires_at <= now
        ]
        for call_id in expired:
            self._items.pop(call_id, None)


def build_browser_prompt_snapshot(payload: dict[str, Any]) -> PromptSnapshot:
    call_id = _required_text(payload.get("call_id"), "call_id")
    _validate_browser_call_id(call_id)
    employee_name = _optional_text(payload.get("employee_name")) or "浏览器测试员工"
    identity_name = _optional_text(payload.get("identityName")) or "项目员工"
    speaking_style = _optional_text(payload.get("speaking_style")) or (
        "电话客服口吻，简短、自然、礼貌但坚定。"
    )
    sections = _normalized_sections(payload.get("sections"))
    instructions = _render_browser_prompt(
        employee_name=employee_name,
        identity_name=identity_name,
        sections=sections,
    )
    return PromptSnapshot(
        scene=BROWSER_PROMPT_SCENE,
        version=BROWSER_PROMPT_VERSION,
        instructions=instructions,
        content_hash=_hash_text(instructions),
        loaded_at_ms=int(time.time() * 1000),
        metadata={
            "source": BROWSER_PROMPT_SCENE,
            "call_id": call_id,
            "employee_name": employee_name,
            "identityName": identity_name,
            "speaking_style": speaking_style,
        },
    )


def _render_browser_prompt(
    *,
    employee_name: str,
    identity_name: str,
    sections: dict[str, list[str]],
) -> str:
    lines = [
        "# 角色",
        f"你是{employee_name}，身份配置为{identity_name}，负责通过浏览器测试实时语音对话效果。",
        "本会话是浏览器测试，不是真实电话外呼；但回复仍必须遵守本轮公共约束。",
        "",
    ]
    for key, title in SECTION_TITLES.items():
        values = sections.get(key, [])
        if not values:
            continue
        lines.append(title)
        lines.extend(f"{index}. {value}" for index, value in enumerate(values, start=1))
        lines.append("")
    return "\n".join(lines).strip()


def _normalized_sections(value: object) -> dict[str, list[str]]:
    raw = value if isinstance(value, dict) else {}
    normalized: dict[str, list[str]] = {}
    for key in SECTION_TITLES:
        section_value = raw.get(key, DEFAULT_SECTION_VALUES[key])
        normalized[key] = _text_list(section_value)
    return normalized


def _text_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [_clean_text(value)] if _clean_text(value) else []
    if not isinstance(value, list):
        return []
    return [
        cleaned
        for item in value
        if (cleaned := _clean_text(item))
    ]


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value)
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _optional_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_text(value: object) -> str:
    return " ".join(str(value).split())


def _validate_browser_call_id(call_id: str) -> None:
    if not call_id.startswith(BROWSER_CALL_ID_PREFIX):
        raise ValueError("browser test call_id must start with browser-")
    if any(char.isspace() for char in call_id) or "/" in call_id:
        raise ValueError("browser test call_id contains unsupported characters")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
```

- [ ] **Step 6: Run store tests and verify GREEN**

Run:

```bash
uv run --with pytest pytest tests/test_browser_prompt_test.py -q
```

Expected: all tests in `tests/test_browser_prompt_test.py` pass.

---

## Task 2: Browser Prompt HTTP Endpoint

**Files:**
- Modify: `app/health_server.py`
- Test: `tests/test_health_server.py`

- [ ] **Step 1: Write failing tests for browser page and prompt registration**

Add imports to `tests/test_health_server.py`:

```python
from urllib.error import HTTPError

from app.browser_prompt_test import BrowserPromptTestStore
```

Add tests after `test_outbound_test_page_is_served()`:

```python
def test_browser_realtime_test_page_is_served():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(
            f"http://{host}:{port}/browser-realtime-test",
            timeout=3,
        ) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "浏览器对话测试" in body
        assert "Prompt Lab" in body
        assert "/browser-test-prompts" in body
        assert "ws://127.0.0.1:9101/media/" in body
        assert "非电话链路验证" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_browser_test_prompts_endpoint_registers_prompt_snapshot():
    prompt_store = BrowserPromptTestStore(ttl_seconds=1800, now=lambda: 100.0)
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(
        config,
        call_manager=FakeCallManager(),
        browser_prompt_store=prompt_store,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/browser-test-prompts",
            data=json.dumps(
                {
                    "call_id": "browser-http-test",
                    "employee_name": "测试员工",
                    "identityName": "项目员工",
                    "speaking_style": "测试客服口吻。",
                    "sections": {"extra": ["HTTP 测试规则。"]},
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "ok"
        assert payload["call_id"] == "browser-http-test"
        assert payload["prompt"]["version"] == "browser-test"
        snapshot = prompt_store.get("browser-http-test")
        assert snapshot is not None
        assert "HTTP 测试规则。" in snapshot.instructions
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_browser_test_prompts_endpoint_rejects_non_browser_call_id():
    prompt_store = BrowserPromptTestStore(ttl_seconds=1800)
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(
        config,
        call_manager=FakeCallManager(),
        browser_prompt_store=prompt_store,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/browser-test-prompts",
            data=json.dumps(
                {"call_id": "real-call-id", "sections": {"extra": ["bad"]}}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as err:
            body = err.read().decode("utf-8")
            payload = json.loads(body)
            assert err.code == 400
            assert payload["status"] == "error"
            assert "browser-" in payload["error"]
        else:
            raise AssertionError("expected non-browser call id to be rejected")
    finally:
        server.shutdown()
        thread.join(timeout=3)
```

- [ ] **Step 2: Run endpoint tests and verify RED**

Run:

```bash
uv run --with pytest pytest \
  tests/test_health_server.py::test_browser_realtime_test_page_is_served \
  tests/test_health_server.py::test_browser_test_prompts_endpoint_registers_prompt_snapshot \
  tests/test_health_server.py::test_browser_test_prompts_endpoint_rejects_non_browser_call_id \
  -q
```

Expected: fail because `HealthServer.__init__()` does not accept `browser_prompt_store` and `/browser-realtime-test` is not served.

- [ ] **Step 3: Modify `HealthServer` constructor and handler wiring**

In `app/health_server.py`, add import:

```python
from .browser_prompt_test import BrowserPromptTestStore
```

Change constructor signature:

```python
class HealthServer:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        call_manager: OutboundCallManager | None = None,
        browser_prompt_store: BrowserPromptTestStore | None = None,
    ):
        self.config = config
        self.call_manager = call_manager
        self.browser_prompt_store = browser_prompt_store
        handler = self._make_handler(
            config,
            call_manager=call_manager,
            browser_prompt_store=browser_prompt_store,
        )
```

Change `_make_handler` signature:

```python
    @staticmethod
    def _make_handler(
        config: GatewayConfig,
        *,
        call_manager: OutboundCallManager | None = None,
        browser_prompt_store: BrowserPromptTestStore | None = None,
    ) -> type[BaseHTTPRequestHandler]:
```

- [ ] **Step 4: Serve browser realtime page**

In `do_GET`, before `/docs` handling, add:

```python
                if parsed.path == "/browser-realtime-test":
                    self._send_html(HTTPStatus.OK, _load_browser_realtime_test_html())
                    return
```

Add loader near `_load_outbound_test_html()`:

```python
def _load_browser_realtime_test_html() -> str:
    html_path = (
        Path(__file__).resolve().parent.parent
        / "static"
        / "browser-realtime-test.html"
    )
    return html_path.read_text(encoding="utf-8")
```

- [ ] **Step 5: Add browser prompt POST endpoint**

In `do_POST`, before `/calls`, add:

```python
                if parsed.path == "/browser-test-prompts":
                    if browser_prompt_store is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {
                                "status": "unavailable",
                                "error": "browser prompt test store disabled",
                            },
                        )
                        return
                    try:
                        snapshot = browser_prompt_store.register(self._read_json_body())
                    except ValueError as err:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"status": "error", "error": str(err)},
                        )
                        return
                    except json.JSONDecodeError:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"status": "error", "error": "invalid JSON body"},
                        )
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "call_id": snapshot.metadata.get("call_id"),
                            "prompt": {
                                "scene": snapshot.scene,
                                "version": snapshot.version,
                                "content_hash": snapshot.content_hash,
                                "loaded_at_ms": snapshot.loaded_at_ms,
                                "metadata": snapshot.metadata,
                            },
                            "expires_in_seconds": browser_prompt_store.ttl_seconds,
                        },
                    )
                    return
```

- [ ] **Step 6: Run endpoint tests and verify page test still fails only because page file is missing**

Run:

```bash
uv run --with pytest pytest \
  tests/test_health_server.py::test_browser_realtime_test_page_is_served \
  tests/test_health_server.py::test_browser_test_prompts_endpoint_registers_prompt_snapshot \
  tests/test_health_server.py::test_browser_test_prompts_endpoint_rejects_non_browser_call_id \
  -q
```

Expected: prompt endpoint tests pass; page test fails with missing `static/browser-realtime-test.html`.

---

## Task 3: Browser Realtime Test Page

**Files:**
- Create: `static/browser-realtime-test.html`
- Modify: `static/outbound-test.html`
- Modify: `app/health_server.py`
- Test: `tests/test_health_server.py`

- [ ] **Step 1: Create browser page HTML shell**

Create `static/browser-realtime-test.html` with:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>浏览器对话测试</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f6f7fb;
        --panel: #ffffff;
        --text: #172033;
        --muted: #667085;
        --line: #d8dee9;
        --accent: #0f766e;
        --danger: #b42318;
        --warn: #b54708;
        --ok: #087443;
        font-family: Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      }
      * { box-sizing: border-box; }
      body { margin: 0; background: var(--bg); color: var(--text); }
      .topbar { border-bottom: 1px solid var(--line); background: #fff; }
      .topbar-inner {
        width: min(1360px, calc(100% - 24px));
        min-height: 64px;
        margin: 0 auto;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
      }
      .brand { display: grid; gap: 2px; }
      .brand strong { font-size: 17px; }
      .brand span { color: var(--muted); font-size: 12px; }
      .nav { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
      .nav a {
        min-height: 36px;
        min-width: 96px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border: 1px solid var(--line);
        border-radius: 6px;
        color: var(--text);
        background: #fff;
        text-decoration: none;
        font-size: 13px;
        font-weight: 600;
      }
      .nav a.active { border-color: #8ccfc0; color: #075c44; background: #e8f5ee; }
      .shell {
        width: min(1360px, calc(100% - 24px));
        margin: 0 auto;
        padding: 18px 0 48px;
      }
      .notice {
        border: 1px solid #fedf89;
        background: #fffaeb;
        color: #7a2e0e;
        border-radius: 8px;
        padding: 12px 14px;
        margin-bottom: 14px;
      }
      .grid {
        display: grid;
        grid-template-columns: minmax(340px, 0.9fr) minmax(420px, 1.1fr);
        gap: 14px;
        align-items: start;
      }
      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 16px;
      }
      h1 { margin: 0 0 8px; font-size: 28px; line-height: 1.25; }
      h2 { margin: 0 0 12px; font-size: 18px; }
      p { margin: 0 0 12px; line-height: 1.6; color: var(--muted); }
      label { display: grid; gap: 6px; margin-bottom: 10px; font-size: 13px; font-weight: 650; }
      input, textarea, select {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 9px 10px;
        font: inherit;
        background: #fff;
        color: var(--text);
      }
      textarea { min-height: 76px; resize: vertical; line-height: 1.45; }
      .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
      button {
        min-height: 38px;
        border: 1px solid var(--line);
        border-radius: 6px;
        background: #fff;
        color: var(--text);
        padding: 0 12px;
        font-weight: 700;
        cursor: pointer;
      }
      button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
      button.danger { color: var(--danger); border-color: #fda29b; }
      button:disabled { opacity: 0.55; cursor: not-allowed; }
      .metrics {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }
      .metric {
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 10px;
        background: #fbfcfe;
      }
      .metric span { display: block; color: var(--muted); font-size: 12px; }
      .metric strong { display: block; margin-top: 3px; font-size: 16px; overflow-wrap: anywhere; }
      pre {
        min-height: 180px;
        max-height: 360px;
        overflow: auto;
        border: 1px solid #263449;
        border-radius: 8px;
        padding: 12px;
        background: #111827;
        color: #d1fadf;
        font-size: 12px;
        line-height: 1.45;
      }
      @media (max-width: 920px) {
        .topbar-inner { align-items: flex-start; flex-direction: column; padding: 10px 0; }
        .nav { width: 100%; justify-content: flex-start; }
        .nav a { flex: 1 1 calc(50% - 4px); }
        .grid { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand">
          <strong>SIP 实时语音网关</strong>
          <span>浏览器 Prompt Lab</span>
        </div>
        <nav class="nav" aria-label="主导航">
          <a href="/outbound-test">外呼测试</a>
          <a class="active" href="/browser-realtime-test" aria-current="page">浏览器对话</a>
          <a href="/docs/handoff">交接文档</a>
          <a href="/docs/notes">学习笔记</a>
          <a href="/docs/mac-softphone">Mac 接入指导</a>
          <a href="/docs/agent-readme">推荐AGENT.md</a>
        </nav>
      </div>
    </header>
    <main class="shell">
      <section class="notice">非电话链路验证：本页面绕过 SIP、RTP、FreeSWITCH 和运营商线路，只用于验证模型对话效果和测试公共约束。</section>
      <section class="grid">
        <section class="panel">
          <h1>浏览器对话测试</h1>
          <p>编辑公共约束后创建测试会话，再连接麦克风。对话中修改不会热更新当前会话，需要断开后创建新的 call_id。</p>
          <h2>连接</h2>
          <label>Call ID <input id="callId" /></label>
          <label>WebSocket URL <input id="wsUrl" /></label>
          <div class="actions">
            <button id="newIdButton" type="button">生成 call_id</button>
            <button id="registerButton" class="primary" type="button">创建测试会话</button>
            <button id="connectButton" class="primary" type="button" disabled>连接并开始说话</button>
            <button id="disconnectButton" class="danger" type="button" disabled>断开</button>
          </div>
          <h2 style="margin-top:16px">状态</h2>
          <div class="metrics">
            <div class="metric"><span>连接状态</span><strong id="connectionStatus">未连接</strong></div>
            <div class="metric"><span>麦克风权限</span><strong id="micStatus">未请求</strong></div>
            <div class="metric"><span>上行帧</span><strong id="uplinkFrames">0</strong></div>
            <div class="metric"><span>下行帧</span><strong id="downlinkFrames">0</strong></div>
            <div class="metric"><span>入站 RMS</span><strong id="micRms">0</strong></div>
            <div class="metric"><span>播放缓冲</span><strong id="playbackBuffer">0 ms</strong></div>
          </div>
        </section>
        <section class="panel">
          <h2>Prompt Lab</h2>
          <label>员工名称 <input id="employeeName" value="浏览器测试员工" /></label>
          <label>身份 <input id="identityName" value="项目员工" /></label>
          <label>speaking_style <textarea id="speakingStyle">电话客服口吻，简短、自然、礼貌但坚定。</textarea></label>
          <label>规则优先级 <textarea data-section="rule_priority">数据库业务策略不得突破身份核实、隐私保护、勿扰终止、支付安全、事实边界和法律红线。</textarea></label>
          <label>高优先级运行红线 <textarea data-section="critical_runtime">要求勿扰、拒绝继续沟通或明确要求不再联系时，必须礼貌结束。</textarea></label>
          <label>对话风格 <textarea data-section="dialog_style">电话回复保持简短自然，每次最多两句。</textarea></label>
          <label>事实边界 <textarea data-section="fact_boundary">不得编造系统未提供的金额、地址、缴费渠道、发票规则、维修进度或司法结果。</textarea></label>
          <label>身份核实与隐私边界 <textarea data-section="privacy_disclosure">未确认本人或授权处理人前，不得披露待处理金额、地址、房号、欠费明细或费用原因。</textarea></label>
          <label>金额与争议处理 <textarea data-section="amount_dispute">金额、减免、分期和部分缴纳只按本轮明确策略说明。</textarea></label>
          <label>物业费场景约束 <textarea data-section="property_fee_scene">用户提出物业服务投诉时，先承接并记录诉求，不在同一回复继续催缴。</textarea></label>
          <label>补充测试规则 <textarea data-section="extra"></textarea></label>
        </section>
      </section>
      <section class="panel" style="margin-top:14px">
        <h2>日志</h2>
        <pre id="logOutput">等待操作...</pre>
      </section>
    </main>
    <script>
      // AudioWorklet capture/playback implementation is added in Task 4.
    </script>
  </body>
</html>
```

- [ ] **Step 2: Run browser page HTTP test and verify GREEN for static content**

Run:

```bash
uv run --with pytest pytest tests/test_health_server.py::test_browser_realtime_test_page_is_served -q
```

Expected: pass.

- [ ] **Step 3: Add navigation link to outbound page**

In `static/outbound-test.html`, add a nav item beside `外呼测试`:

```html
<a href="/browser-realtime-test">浏览器对话</a>
```

- [ ] **Step 4: Add navigation link to document shell**

In `app/health_server.py`, update `_document_shell()` `nav_items`:

```python
    nav_items = [
        ("/outbound-test", "外呼测试", None),
        ("/browser-realtime-test", "浏览器对话", None),
        ("/docs/handoff", "交接文档", "handoff"),
        ("/docs/notes", "学习笔记", "notes"),
        ("/docs/mac-softphone", "Mac 接入指导", "mac-softphone"),
        ("/docs/agent-readme", "推荐AGENT.md", "agent-readme"),
    ]
```

- [ ] **Step 5: Update existing navigation assertions**

In `tests/test_health_server.py`, update page tests that assert nav links so they also include:

```python
assert "浏览器对话" in body
assert 'href="/browser-realtime-test"' in body
```

If a test asserts exact counts for nav links, add:

```python
assert body.count('href="/browser-realtime-test"') == 1
```

- [ ] **Step 6: Run health server tests and verify GREEN**

Run:

```bash
uv run --with pytest pytest tests/test_health_server.py -q
```

Expected: all health server tests pass.

---

## Task 4: Main Wiring and Prompt Provider Precedence

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_realtime_phone_gateway.py` or `tests/test_main.py`

- [ ] **Step 1: Write a provider precedence test**

Add to `tests/test_main.py`:

```python
from app.main import _browser_first_prompt_snapshot_provider
from app.postgres import PromptSnapshot


def test_browser_first_prompt_snapshot_provider_prefers_browser_store():
    browser_snapshot = PromptSnapshot(
        scene="browser-realtime-test",
        version="browser-test",
        instructions="browser prompt",
        content_hash="browser-hash",
        loaded_at_ms=1,
        metadata={"source": "browser-realtime-test"},
    )
    outbound_snapshot = PromptSnapshot(
        scene="default",
        version="postgres",
        instructions="outbound prompt",
        content_hash="outbound-hash",
        loaded_at_ms=2,
        metadata={"source": "postgres"},
    )

    class Store:
        def get(self, call_id):
            return browser_snapshot if call_id == "browser-1" else None

    def outbound_provider(call_id):
        return outbound_snapshot if call_id == "real-1" else None

    provider = _browser_first_prompt_snapshot_provider(Store(), outbound_provider)

    assert provider("browser-1") is browser_snapshot
    assert provider("real-1") is outbound_snapshot
    assert provider("missing") is None
```

- [ ] **Step 2: Run provider test and verify RED**

Run:

```bash
uv run --with pytest pytest tests/test_main.py::test_browser_first_prompt_snapshot_provider_prefers_browser_store -q
```

Expected: fail because `_browser_first_prompt_snapshot_provider` does not exist.

- [ ] **Step 3: Implement provider helper and main wiring**

In `app/main.py`, add import:

```python
from .browser_prompt_test import BrowserPromptTestStore
```

In `_serve()`, after `opening_store = OpeningAudioStore()`:

```python
    browser_prompt_store = BrowserPromptTestStore()
```

Pass it to `HealthServer`:

```python
    health_server = HealthServer(
        config,
        call_manager=outbound_manager,
        browser_prompt_store=browser_prompt_store,
    )
```

Pass composed provider to `FreeSwitchRealtimeGatewayServer`:

```python
            prompt_snapshot_provider=_browser_first_prompt_snapshot_provider(
                browser_prompt_store,
                outbound_manager.get_prompt_snapshot,
            ),
```

Add helper near `_system_prompt_for_doubao_session()`:

```python
def _browser_first_prompt_snapshot_provider(browser_prompt_store, outbound_provider):
    def provider(call_id: str):
        snapshot = browser_prompt_store.get(call_id)
        if snapshot is not None:
            return snapshot
        return outbound_provider(call_id)

    return provider
```

- [ ] **Step 4: Run provider test and verify GREEN**

Run:

```bash
uv run --with pytest pytest tests/test_main.py::test_browser_first_prompt_snapshot_provider_prefers_browser_store -q
```

Expected: pass.

- [ ] **Step 5: Run main tests**

Run:

```bash
uv run --with pytest pytest tests/test_main.py -q
```

Expected: all main tests pass.

---

## Task 5: Browser Audio Capture and Playback

**Files:**
- Modify: `static/browser-realtime-test.html`
- Test: manual browser verification plus existing backend tests

- [ ] **Step 1: Implement call ID and prompt registration JavaScript**

Replace the placeholder script in `static/browser-realtime-test.html` with:

```html
<script>
  const callIdInput = document.getElementById("callId");
  const wsUrlInput = document.getElementById("wsUrl");
  const newIdButton = document.getElementById("newIdButton");
  const registerButton = document.getElementById("registerButton");
  const connectButton = document.getElementById("connectButton");
  const disconnectButton = document.getElementById("disconnectButton");
  const connectionStatus = document.getElementById("connectionStatus");
  const micStatus = document.getElementById("micStatus");
  const uplinkFrames = document.getElementById("uplinkFrames");
  const downlinkFrames = document.getElementById("downlinkFrames");
  const micRms = document.getElementById("micRms");
  const playbackBuffer = document.getElementById("playbackBuffer");
  const logOutput = document.getElementById("logOutput");
  const employeeNameInput = document.getElementById("employeeName");
  const identityNameInput = document.getElementById("identityName");
  const speakingStyleInput = document.getElementById("speakingStyle");

  let ws = null;
  let audioContext = null;
  let mediaStream = null;
  let sourceNode = null;
  let uplinkCount = 0;
  let downlinkCount = 0;
  let promptRegistered = false;
  const playbackQueue = [];

  function log(message) {
    const time = new Date().toLocaleTimeString();
    logOutput.textContent = `[${time}] ${message}\n` + logOutput.textContent;
  }

  function makeCallId() {
    const stamp = new Date().toISOString().replace(/\D/g, "").slice(0, 14);
    const random = Math.random().toString(16).slice(2, 8);
    return `browser-${stamp}-${random}`;
  }

  function updateWsUrl() {
    const host = location.hostname || "127.0.0.1";
    const callId = callIdInput.value.trim();
    wsUrlInput.value = `ws://${host}:9101/media/${callId}`;
  }

  function sectionLines(name) {
    const value = document.querySelector(`[data-section="${name}"]`).value;
    return value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function promptPayload() {
    return {
      call_id: callIdInput.value.trim(),
      employee_name: employeeNameInput.value.trim(),
      identityName: identityNameInput.value.trim(),
      speaking_style: speakingStyleInput.value.trim(),
      sections: {
        rule_priority: sectionLines("rule_priority"),
        critical_runtime: sectionLines("critical_runtime"),
        dialog_style: sectionLines("dialog_style"),
        fact_boundary: sectionLines("fact_boundary"),
        privacy_disclosure: sectionLines("privacy_disclosure"),
        amount_dispute: sectionLines("amount_dispute"),
        property_fee_scene: sectionLines("property_fee_scene"),
        extra: sectionLines("extra"),
      },
    };
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.error || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  newIdButton.addEventListener("click", () => {
    callIdInput.value = makeCallId();
    promptRegistered = false;
    connectButton.disabled = true;
    updateWsUrl();
    log("已生成新的 browser call_id");
  });

  callIdInput.addEventListener("input", () => {
    promptRegistered = false;
    connectButton.disabled = true;
    updateWsUrl();
  });

  registerButton.addEventListener("click", async () => {
    try {
      updateWsUrl();
      const payload = await postJson("/browser-test-prompts", promptPayload());
      promptRegistered = true;
      connectButton.disabled = false;
      log(`测试会话已创建：${payload.call_id} / ${payload.prompt.content_hash}`);
    } catch (error) {
      promptRegistered = false;
      connectButton.disabled = true;
      log(`创建测试会话失败：${error.message}`);
    }
  });

  callIdInput.value = makeCallId();
  updateWsUrl();
</script>
```

- [ ] **Step 2: Add PCM conversion helpers**

Inside the same script, before event listeners:

```javascript
  function floatToInt16Pcm(floatSamples) {
    const buffer = new ArrayBuffer(floatSamples.length * 2);
    const view = new DataView(buffer);
    for (let index = 0; index < floatSamples.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, floatSamples[index]));
      view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    }
    return buffer;
  }

  function pcmToFloat32(buffer) {
    const view = new DataView(buffer);
    const samples = new Float32Array(buffer.byteLength / 2);
    for (let index = 0; index < samples.length; index += 1) {
      samples[index] = view.getInt16(index * 2, true) / 0x8000;
    }
    return samples;
  }

  function rms(samples) {
    if (!samples.length) return 0;
    let sum = 0;
    for (const sample of samples) sum += sample * sample;
    return Math.sqrt(sum / samples.length);
  }
```

- [ ] **Step 3: Add AudioWorklet code as a Blob**

Inside the script, add:

```javascript
  function captureWorkletUrl() {
    const code = `
      class CaptureProcessor extends AudioWorkletProcessor {
        constructor() {
          super();
          this.pending = [];
          this.sourceRate = sampleRate;
          this.targetRate = 8000;
          this.step = this.sourceRate / this.targetRate;
          this.cursor = 0;
        }
        process(inputs) {
          const input = inputs[0];
          if (!input || !input[0]) return true;
          const left = input[0];
          const right = input[1];
          const mono = new Float32Array(left.length);
          for (let i = 0; i < left.length; i++) {
            mono[i] = right ? (left[i] + right[i]) / 2 : left[i];
          }
          while (this.cursor < mono.length) {
            const index = Math.floor(this.cursor);
            this.pending.push(mono[index] || 0);
            this.cursor += this.step;
            if (this.pending.length === 160) {
              const frame = new Float32Array(this.pending);
              this.pending = [];
              this.port.postMessage(frame, [frame.buffer]);
            }
          }
          this.cursor -= mono.length;
          return true;
        }
      }
      registerProcessor("capture-processor", CaptureProcessor);
    `;
    return URL.createObjectURL(new Blob([code], { type: "application/javascript" }));
  }
```

- [ ] **Step 4: Add WebSocket connect and microphone streaming**

Inside the script, add:

```javascript
  async function connectMedia() {
    if (!promptRegistered) {
      log("请先创建测试会话");
      return;
    }
    connectionStatus.textContent = "连接中";
    audioContext = new AudioContext();
    await audioContext.audioWorklet.addModule(captureWorkletUrl());
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    micStatus.textContent = "已授权";
    sourceNode = audioContext.createMediaStreamSource(mediaStream);
    const captureNode = new AudioWorkletNode(audioContext, "capture-processor");
    sourceNode.connect(captureNode);
    ws = new WebSocket(wsUrlInput.value.trim());
    ws.binaryType = "arraybuffer";
    ws.addEventListener("open", () => {
      connectionStatus.textContent = "已连接";
      disconnectButton.disabled = false;
      connectButton.disabled = true;
      log("媒体 WebSocket 已连接");
    });
    ws.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        log(`控制消息：${event.data}`);
        return;
      }
      downlinkCount += 1;
      downlinkFrames.textContent = String(downlinkCount);
      playbackQueue.push(pcmToFloat32(event.data));
      playbackBuffer.textContent = `${Math.round(playbackQueue.length * 20)} ms`;
      schedulePlayback();
    });
    ws.addEventListener("close", () => {
      connectionStatus.textContent = "已断开";
      disconnectButton.disabled = true;
      connectButton.disabled = false;
      log("媒体 WebSocket 已断开");
    });
    ws.addEventListener("error", () => {
      connectionStatus.textContent = "错误";
      log("媒体 WebSocket 错误");
    });
    captureNode.port.onmessage = (event) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (ws.bufferedAmount > 320 * 20) return;
      const samples = event.data;
      micRms.textContent = rms(samples).toFixed(4);
      ws.send(floatToInt16Pcm(samples));
      uplinkCount += 1;
      uplinkFrames.textContent = String(uplinkCount);
    };
  }
```

- [ ] **Step 5: Add simple playback scheduler**

Inside the script, add:

```javascript
  let nextPlaybackAt = 0;

  function schedulePlayback() {
    if (!audioContext) return;
    if (playbackQueue.length < 4) return;
    while (playbackQueue.length) {
      const frame = playbackQueue.shift();
      const buffer = audioContext.createBuffer(1, frame.length, 8000);
      buffer.copyToChannel(frame, 0);
      const node = audioContext.createBufferSource();
      node.buffer = buffer;
      node.connect(audioContext.destination);
      const now = audioContext.currentTime;
      nextPlaybackAt = Math.max(nextPlaybackAt, now + 0.08);
      node.start(nextPlaybackAt);
      nextPlaybackAt += frame.length / 8000;
    }
    playbackBuffer.textContent = `${Math.round(playbackQueue.length * 20)} ms`;
  }
```

This scheduler is acceptable for MVP. If playback clicks or gaps are observed, replace it with a playback `AudioWorkletProcessor` and ring buffer in a follow-up patch.

- [ ] **Step 6: Add connect and disconnect handlers**

Inside the script, add:

```javascript
  connectButton.addEventListener("click", async () => {
    try {
      await connectMedia();
    } catch (error) {
      connectionStatus.textContent = "错误";
      micStatus.textContent = "失败";
      log(`连接失败：${error.message}`);
      await disconnectMedia();
    }
  });

  disconnectButton.addEventListener("click", () => {
    disconnectMedia();
  });

  async function disconnectMedia() {
    if (ws) {
      ws.close();
      ws = null;
    }
    if (mediaStream) {
      for (const track of mediaStream.getTracks()) track.stop();
      mediaStream = null;
    }
    if (audioContext) {
      await audioContext.close().catch(() => {});
      audioContext = null;
    }
    sourceNode = null;
    playbackQueue.length = 0;
    nextPlaybackAt = 0;
    disconnectButton.disabled = true;
    connectButton.disabled = !promptRegistered;
    connectionStatus.textContent = "已断开";
    playbackBuffer.textContent = "0 ms";
  }
```

- [ ] **Step 7: Run health server tests**

Run:

```bash
uv run --with pytest pytest tests/test_health_server.py -q
```

Expected: pass.

---

## Task 6: Realtime Gateway Regression Coverage

**Files:**
- Modify: `tests/test_realtime_phone_gateway.py`

- [ ] **Step 1: Check existing provider precedence coverage**

Inspect the existing test:

```bash
rg -n "prefers_prebuilt_prompt_snapshot|prompt_snapshot_provider" tests/test_realtime_phone_gateway.py
```

Expected: existing coverage should show that `prompt_snapshot_provider` is used before legacy prompt store.

- [ ] **Step 2: Add browser-specific assertion only if needed**

If there is no test proving provider precedence, add:

```python
def test_realtime_gateway_prefers_browser_prompt_snapshot_provider():
    snapshot = PromptSnapshot(
        scene="browser-realtime-test",
        version="browser-test",
        instructions="浏览器测试提示词",
        content_hash="browser-hash",
        loaded_at_ms=1,
        metadata={"employee_name": "浏览器测试员工", "identityName": "项目员工"},
    )
    server = FreeSwitchRealtimeGatewayServer(
        _test_config(tail_silence_ms=0),
        api_key="test-key",
        prompt_snapshot_provider=lambda call_id: snapshot
        if call_id == "browser-test-call"
        else None,
    )
    session = RealtimePhoneSessionStats(
        call_id="browser-test-call",
        session_id="session-1",
        connected_at=0,
        last_seen_at=0,
        expected_frame_bytes=320,
    )

    loaded = asyncio.run(server._load_prompt_snapshot(session))

    assert loaded is snapshot
```

- [ ] **Step 3: Run realtime provider tests**

Run:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id -q
```

If a browser-specific test was added, run it too:

```bash
uv run --with pytest pytest tests/test_realtime_phone_gateway.py::test_realtime_gateway_prefers_browser_prompt_snapshot_provider -q
```

Expected: pass.

---

## Task 7: Documentation and Verification

**Files:**
- Modify: `docs/browser-realtime-gateway-test-technical-design.md`

- [ ] **Step 1: Confirm design document matches implemented endpoints**

Check:

```bash
rg -n "browser-test-prompts|browser-realtime-test|BrowserPromptTestStore|browser-" docs/browser-realtime-gateway-test-technical-design.md
```

Expected: document mentions `GET /browser-realtime-test`, `POST /browser-test-prompts`, `BrowserPromptTestStore`, and browser-only `call_id`.

- [ ] **Step 2: Run targeted tests**

Run:

```bash
uv run --with pytest pytest \
  tests/test_browser_prompt_test.py \
  tests/test_health_server.py \
  tests/test_main.py \
  tests/test_realtime_phone_gateway.py::test_realtime_gateway_prefers_prebuilt_prompt_snapshot_by_call_id \
  -q
```

Expected: all targeted tests pass.

- [ ] **Step 3: Run full gateway test suite**

Run:

```bash
uv run --with pytest pytest -q
```

Expected: all tests pass. If unrelated dirty worktree changes make the full suite fail, record the exact failing test names and rerun the targeted suite to confirm this feature remains green.

- [ ] **Step 4: Manual browser smoke test**

Start the local realtime gateway:

```bash
scripts/dev-local.sh restart
scripts/dev-local.sh check
```

Open:

```text
http://127.0.0.1:9100/browser-realtime-test
```

Manual steps:

```text
1. Generate a browser call_id.
2. Edit one visible public constraint, for example add “回答前先确认这是浏览器测试” to extra.
3. Click 创建测试会话.
4. Click 连接并开始说话.
5. Speak a short test utterance.
6. Confirm AI audio is heard.
7. Confirm gateway logs include `prebuilt_prompt_snapshot_loaded` with version `browser-test`.
8. Confirm there is no repeated `frame_size_mismatch`.
```

- [ ] **Step 5: Commit implementation**

Only commit after targeted tests and manual smoke test status are known.

```bash
git add \
  app/browser_prompt_test.py \
  app/health_server.py \
  app/main.py \
  static/browser-realtime-test.html \
  static/outbound-test.html \
  tests/test_browser_prompt_test.py \
  tests/test_health_server.py \
  tests/test_main.py \
  tests/test_realtime_phone_gateway.py \
  docs/browser-realtime-gateway-test-technical-design.md
git commit -m "feat: add browser realtime prompt lab"
```

---

## Self-Review

Spec coverage:

```text
浏览器直连测试页：Task 3 and Task 5
开始前动态编辑公共约束：Task 1, Task 2, Task 3
只影响 browser call_id：Task 1 and Task 4
不污染真实外呼：Task 1 validation and Task 4 provider ordering
媒体协议复用 8k PCM WebSocket：Task 5
测试和验证：Task 1, Task 2, Task 4, Task 6, Task 7
```

Placeholder scan:

```text
The plan contains no unfinished placeholders, vague future-work markers, or unspecified generic test steps.
```

Type consistency:

```text
`BrowserPromptTestStore.register(payload) -> PromptSnapshot`
`BrowserPromptTestStore.get(call_id) -> PromptSnapshot | None`
`POST /browser-test-prompts`
`GET /browser-realtime-test`
`_browser_first_prompt_snapshot_provider(store, outbound_provider)`
```
