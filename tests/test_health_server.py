from __future__ import annotations

import json
import threading
from urllib.request import Request, urlopen

from app.config import GatewayConfig, ServerConfig
from app.health_server import HealthServer


def test_health_endpoint_returns_ok():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/health", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "ok"
        assert payload["service"] == "sip-realtime-voice-gateway"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_ready_endpoint_does_not_expose_api_keys():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/ready", timeout=3) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body)

        assert response.status == 200
        assert payload["status"] == "ready"
        assert "api_key" not in body.lower()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_calls_endpoint_accepts_outbound_request():
    manager = FakeCallManager()
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls",
            data=json.dumps({"destination": "1000"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 202
        assert payload["status"] == "accepted"
        assert payload["call"]["call_id"] == "call-1"
        assert manager.created_payload == {"destination": "1000"}
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_call_status_endpoint_returns_call():
    manager = FakeCallManager()
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/calls/call-1", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["call"]["status"] == "queued"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_outbound_test_page_is_served():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/outbound-test", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "SIP 实时语音网关外呼测试" in body
        assert "当前测试链路" not in body
        assert "真实商用时替换" in body
        assert 'id="callerNumber"' in body
        assert 'value="9000"' in body
        assert 'id="callerName"' in body
        assert 'value="AI_Assistant"' in body
        assert 'value="AI Agent"' not in body
        assert "POST /calls" in body
        assert 'class="workspace"' in body
        assert body.index('class="system-panel"') < body.index('class="workspace"')
        assert body.index('class="dial-panel"') > body.index('class="workspace"')
        assert "页面接口" in body
        assert "这里只表示本页面 HTTP 控制接口是否可响应" in body
        assert "Dialplan Extension（接通后入口）" in body
        assert "Dialplan Context（拨号上下文）" in body
        assert "业务标记（日志标记，不影响拨号）" in body
        assert "ready 表示页面接口可用" not in body
        assert "外呼测试" in body
        assert "交接文档" in body
        assert "学习笔记" in body
        assert "Mac 接入指导" in body
        assert "推荐AGENT.md" in body
        assert 'href="/outbound-test"' in body
        assert 'nav class="nav" aria-label="主导航"' in body
        assert "width: min(1360px, calc(100% - 24px))" in body
        assert "width: min(100% - 16px, 1360px)" in body
        assert body.index("刷新状态") > body.index('<main class="shell">')
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
        assert body.count('href="/docs/handoff"') == 1
        assert body.count('href="/docs/notes"') == 1
        assert body.count('href="/docs/mac-softphone"') == 1
        assert body.count('href="/docs/agent-readme"') == 1
        assert "项目文档" not in body
        assert "文档入口" not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_root_serves_outbound_test_page():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "SIP 实时语音网关外呼测试" in body
        assert "外呼测试" in body
        assert "交接文档" in body
        assert "学习笔记" in body
        assert "Mac 接入指导" in body
        assert "推荐AGENT.md" in body
        assert 'href="/outbound-test"' in body
        assert 'nav class="nav" aria-label="主导航"' in body
        assert "width: min(1360px, calc(100% - 24px))" in body
        assert "width: min(100% - 16px, 1360px)" in body
        assert body.index("刷新状态") > body.index('<main class="shell">')
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
        assert body.count('href="/docs/handoff"') == 1
        assert body.count('href="/docs/notes"') == 1
        assert body.count('href="/docs/mac-softphone"') == 1
        assert body.count('href="/docs/agent-readme"') == 1
        assert "项目文档" not in body
        assert "文档入口" not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_docs_path_redirects_to_handoff_doc():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/docs", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert response.geturl().endswith("/docs/handoff")
        assert '<h1 id="section-1">SIP 实时语音网关交接总文档</h1>' in body
        assert "文档入口" not in body
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_handoff_doc_is_rendered_as_html():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/docs/handoff", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert '<h1 id="section-1">SIP 实时语音网关交接总文档</h1>' in body
        assert "外呼测试" in body
        assert "SIP 实时语音网关" in body
        assert 'nav class="nav" aria-label="主导航"' in body
        assert 'href="/outbound-test"' in body
        assert 'class="active" aria-current="page" href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
        assert "刷新状态" not in body
        assert 'class="doc-layout"' in body
        assert 'class="doc-toc"' in body
        assert '<strong class="doc-toc-title">目录</strong>' in body
        assert 'href="#section-2">1. 当前结论</a>' in body
        assert 'href="#section-5">3.1 本地测试链路</a>' not in body
        assert '<h2 id="section-2">1. 当前结论</h2>' in body
        assert "SIP实时语音网关交接总文档.md" not in body
        assert "TEN电话接入学习笔记.md" not in body
        assert "业务需求" in body
        assert "状态机" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_notes_doc_is_rendered_from_static_html():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/docs/notes", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert '<h1 id="section-1">TEN 电话线路接入学习笔记</h1>' in body
        assert 'href="/outbound-test"' in body
        assert 'href="/docs/handoff"' in body
        assert 'class="active" aria-current="page" href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
        assert "刷新状态" not in body
        assert 'class="doc-layout"' in body
        assert 'class="doc-toc"' in body
        assert 'href="#section-2">1. 一句话理解</a>' in body
        assert 'href="#section-4">SIP</a>' not in body
        assert '<h2 id="section-2">1. 一句话理解</h2>' in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_mac_softphone_doc_is_rendered_from_static_html():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/docs/mac-softphone", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert '<h1 id="section-1">Mac 软电话接入 9199 本地测试指导</h1>' in body
        assert 'href="/outbound-test"' in body
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert (
            'class="active" aria-current="page" href="/docs/mac-softphone"' in body
        )
        assert 'href="/docs/agent-readme"' in body
        assert "刷新状态" not in body
        assert 'class="doc-layout"' in body
        assert 'class="doc-toc"' in body
        assert 'href="#section-2">结论</a>' in body
        assert 'href="#section-5">1. FreeSWITCH 本地地址</a>' not in body
        assert '<h2 id="section-2">结论</h2>' in body
        assert "必须保证原有 Windows / MicroSIP 测试链路仍然可用" in body
        assert "Windows / MicroSIP 原有链路仍然可以注册、拨入 9199" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_agent_readme_doc_is_rendered_from_static_html():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/docs/agent-readme", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "<title>推荐 AGENT.md 内容</title>" in body
        assert '<h1 id="section-1">推荐 AGENT.md 内容</h1>' in body
        assert 'href="#section-2">1. 沟通方式</a>' in body
        assert 'href="#section-3">2. 分析方式</a>' in body
        assert 'href="#section-4">3. 执行方式</a>' in body
        assert 'href="#section-5">4. 文档要求</a>' in body
        assert 'href="#section-6">5. Git 要求</a>' in body
        assert 'href="#section-7">6. 当前项目特别要求</a>' in body
        assert 'href="/outbound-test"' in body
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'class="active" aria-current="page" href="/docs/agent-readme"' in body
        assert "AI 协作 README" not in body
        assert "这份文档沉淀" not in body
        assert "不是业务交接文档" not in body
        assert "先基于事实拆清楚链路和根因" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_top_navigation_stays_stable_across_pages():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/outbound-test", timeout=3) as response:
            outbound = response.read().decode("utf-8")
        with urlopen(f"http://{host}:{port}/docs/handoff", timeout=3) as response:
            handoff = response.read().decode("utf-8")
        with urlopen(f"http://{host}:{port}/docs/notes", timeout=3) as response:
            notes = response.read().decode("utf-8")
        with urlopen(f"http://{host}:{port}/docs/mac-softphone", timeout=3) as response:
            mac = response.read().decode("utf-8")
        with urlopen(f"http://{host}:{port}/docs/agent-readme", timeout=3) as response:
            agent = response.read().decode("utf-8")

        assert _topbar_without_current_page(outbound) == _topbar_without_current_page(
            handoff
        )
        assert _topbar_without_current_page(handoff) == _topbar_without_current_page(
            notes
        )
        assert _topbar_without_current_page(notes) == _topbar_without_current_page(mac)
        assert _topbar_without_current_page(mac) == _topbar_without_current_page(agent)
        assert outbound.index("刷新状态") > outbound.index("</header>")
        assert "刷新状态" not in handoff
        assert "刷新状态" not in notes
        assert "刷新状态" not in mac
        assert "刷新状态" not in agent
    finally:
        server.shutdown()
        thread.join(timeout=3)


def _topbar_without_current_page(body: str) -> str:
    start = body.index('<header class="topbar">')
    end = body.index("</header>", start) + len("</header>")
    topbar = (
        body[start:end]
        .replace(' class="active"', "")
        .replace(' aria-current="page"', "")
    )
    topbar = " ".join(topbar.split())
    return topbar.replace("> <", "><")


class FakeCallManager:
    def __init__(self) -> None:
        self.created_payload = None

    def create_call(self, payload):
        self.created_payload = payload
        return {"call_id": "call-1", "status": "queued"}

    def list_calls(self, *, limit=50):
        return [self.get_call("call-1")]

    def get_call(self, call_id):
        if call_id != "call-1":
            return None
        return {"call_id": "call-1", "status": "queued"}

    def request_hangup(self, call_id, *, cause="NORMAL_CLEARING"):
        return {"call_id": call_id, "status": "hangup_requested", "cause": cause}
