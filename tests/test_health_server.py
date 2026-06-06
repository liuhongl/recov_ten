from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.browser_prompt_test import BrowserPromptTestStore
from app.config import (
    CallRecordingConfig,
    GatewayConfig,
    HandoffConfig,
    HumanTranscriptConfig,
    RocketMQAclConfig,
    RocketMQConfig,
    ServerConfig,
)
from app.health_server import HealthServer
from app.postgres import BusinessPromptPreparation, PromptSnapshot


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


def test_ready_endpoint_exposes_rocketmq_non_secret_config():
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        rocketmq=RocketMQConfig(
            enabled=True,
            endpoint="http://mq.example/",
            name_server="mq.example:9876",
            producer_group="recov-ten-gateway",
            callback_topic="recov-flow-callback",
            send_timeout_ms=4500,
            acl=RocketMQAclConfig(
                enabled=True,
                access_key_env="ROCKETMQ_ACCESS_KEY",
                secret_key_env="ROCKETMQ_SECRET_KEY",
                security_token_env="ROCKETMQ_SECURITY_TOKEN",
            ),
        ),
    )
    server = HealthServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/ready", timeout=3) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body)

        assert response.status == 200
        assert payload["config"]["rocketmq"] == {
            "enabled": True,
            "endpoint": "http://mq.example/",
            "name_server": "mq.example:9876",
            "producer_group": "recov-ten-gateway",
            "callback_topic": "recov-flow-callback",
            "send_timeout_ms": 4500,
            "acl": {
                "enabled": True,
                "access_key_env": "ROCKETMQ_ACCESS_KEY",
                "secret_key_env": "ROCKETMQ_SECRET_KEY",
                "security_token_env": "ROCKETMQ_SECURITY_TOKEN",
            },
        }
        assert "secret-value" not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_ready_endpoint_exposes_human_transcript_config():
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        human_transcript=HumanTranscriptConfig(
            enabled=True,
            provider="http_json",
            http_url="http://127.0.0.1:9220/transcribe",
            timeout_seconds=12.5,
        ),
    )
    server = HealthServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/ready", timeout=3) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body)

        assert response.status == 200
        assert payload["config"]["human_transcript"] == {
            "enabled": True,
            "provider": "http_json",
            "http_url": "http://127.0.0.1:9220/transcribe",
            "timeout_seconds": 12.5,
        }
        assert "test-secret-value" not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_ready_endpoint_exposes_handoff_config():
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        handoff=HandoffConfig(wait_timeout_seconds=12),
    )
    server = HealthServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/ready", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["config"]["handoff"] == {"wait_timeout_seconds": 12}
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
        assert payload["accepted"] is True
        assert payload["businessId"] == "call-1"
        assert payload["message"] == "AI外呼任务已受理"
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


def test_call_recording_endpoint_serves_wav_from_host_directory(tmp_path):
    recording_file = tmp_path / "20260603" / "call-1.wav"
    recording_file.parent.mkdir()
    recording_file.write_bytes(b"RIFF-test-wav")
    manager = FakeCallManager()
    manager.calls = [
        {
            "call_id": "call-1",
            "status": "completed",
            "recording_path": "/var/lib/freeswitch/recordings/20260603/call-1.wav",
        }
    ]
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        call_recording=CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            host_directory=str(tmp_path),
        ),
    )
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(
            f"http://{host}:{port}/calls/call-1/recording",
            timeout=3,
        ) as response:
            body = response.read()

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("audio/wav")
        assert response.headers["Cache-Control"] == "no-store"
        assert body == b"RIFF-test-wav"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_call_recording_endpoint_supports_byte_range_requests(tmp_path):
    recording_file = tmp_path / "20260603" / "call-1.wav"
    recording_file.parent.mkdir()
    recording_file.write_bytes(b"RIFF-test-wav")
    manager = FakeCallManager()
    manager.calls = [
        {
            "call_id": "call-1",
            "status": "completed",
            "recording_path": "/var/lib/freeswitch/recordings/20260603/call-1.wav",
        }
    ]
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        call_recording=CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            host_directory=str(tmp_path),
        ),
    )
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls/call-1/recording",
            headers={"Range": "bytes=0-3"},
        )
        with urlopen(request, timeout=3) as response:
            body = response.read()

        assert response.status == 206
        assert response.headers["Content-Type"].startswith("audio/wav")
        assert response.headers["Accept-Ranges"] == "bytes"
        assert response.headers["Content-Range"] == "bytes 0-3/13"
        assert body == b"RIFF"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_call_recording_endpoint_supports_head_requests(tmp_path):
    recording_file = tmp_path / "20260603" / "call-1.wav"
    recording_file.parent.mkdir()
    recording_file.write_bytes(b"RIFF-test-wav")
    manager = FakeCallManager()
    manager.calls = [
        {
            "call_id": "call-1",
            "status": "completed",
            "recording_path": "/var/lib/freeswitch/recordings/20260603/call-1.wav",
        }
    ]
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        call_recording=CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            host_directory=str(tmp_path),
        ),
    )
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls/call-1/recording",
            method="HEAD",
        )
        with urlopen(request, timeout=3) as response:
            body = response.read()

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("audio/wav")
        assert response.headers["Accept-Ranges"] == "bytes"
        assert response.headers["Content-Length"] == "13"
        assert body == b""
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_call_recording_endpoint_rejects_invalid_byte_range(tmp_path):
    recording_file = tmp_path / "20260603" / "call-1.wav"
    recording_file.parent.mkdir()
    recording_file.write_bytes(b"RIFF-test-wav")
    manager = FakeCallManager()
    manager.calls = [
        {
            "call_id": "call-1",
            "status": "completed",
            "recording_path": "/var/lib/freeswitch/recordings/20260603/call-1.wav",
        }
    ]
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        call_recording=CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            host_directory=str(tmp_path),
        ),
    )
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls/call-1/recording",
            headers={"Range": "bytes=99-120"},
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as err:
            assert err.code == 416
            assert err.headers["Content-Range"] == "bytes */13"
            assert err.read() == b""
        else:
            raise AssertionError("expected invalid byte range to return 416")
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_call_recording_endpoint_returns_404_when_recording_is_missing(tmp_path):
    manager = FakeCallManager()
    manager.calls = [
        {
            "call_id": "call-1",
            "status": "completed",
            "recording_path": "/var/lib/freeswitch/recordings/20260603/call-1.wav",
        }
    ]
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        call_recording=CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            host_directory=str(tmp_path),
        ),
    )
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        try:
            urlopen(f"http://{host}:{port}/calls/call-1/recording", timeout=3)
        except HTTPError as err:
            payload = json.loads(err.read().decode("utf-8"))
            assert err.code == 404
            assert payload["status"] == "error"
            assert "recording file not found" in payload["error"]
        else:
            raise AssertionError("expected missing recording file to return 404")
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_call_recording_endpoint_rejects_recording_path_outside_directory(tmp_path):
    outside_file = tmp_path.parent / "outside.wav"
    outside_file.write_bytes(b"RIFF-outside")
    manager = FakeCallManager()
    manager.calls = [
        {
            "call_id": "call-1",
            "status": "completed",
            "recording_path": "/var/lib/freeswitch/outside.wav",
        }
    ]
    config = GatewayConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        call_recording=CallRecordingConfig(
            enabled=True,
            directory="/var/lib/freeswitch/recordings",
            host_directory=str(tmp_path),
        ),
    )
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        try:
            urlopen(f"http://{host}:{port}/calls/call-1/recording", timeout=3)
        except HTTPError as err:
            payload = json.loads(err.read().decode("utf-8"))
            assert err.code == 400
            assert payload["status"] == "error"
            assert "outside call_recording.directory" in payload["error"]
        else:
            raise AssertionError("expected unsafe recording path to return 400")
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
        assert "数据库业务参数" in body
        assert '<option value="manual-sip-provider" selected>' in body
        assert '<option value="sandbox-answer" selected>' not in body
        assert '"local-1000": {' in body
        assert 'endpoint: "sofia_contact:*/1000",' in body
        assert "sipProviderEndpoint" in body
        assert "syncDynamicEndpoint" in body
        assert 'id="callId"' in body
        assert 'name="callId"' in body
        assert "通话记录 callId" in body
        assert 'id="taskId"' in body
        assert 'name="taskId"' in body
        assert "流程任务 taskId" in body
        assert "function syncBusinessFieldsForSubmit()" in body
        assert "let recentCalls = [];" in body
        assert "function isLocalOutboundTest()" in body
        assert "function generateLocalBusinessId()" in body
        assert "function hasTerminalRecentCallForBusinessId(businessId)" in body
        assert "function ensureFreshLocalBusinessIdsForSubmit()" in body
        assert "recentCalls = calls;" in body
        assert "ensureFreshLocalBusinessIdsForSubmit();" in body
        assert "function applyQueryOverrides()" in body
        assert "let appliedQuerySearch = null;" in body
        assert "function applyPendingQueryOverrides()" in body
        assert "if (window.location.search !== appliedQuerySearch)" in body
        assert "new URLSearchParams(window.location.search)" in body
        assert "syncBusinessFieldsForSubmit();" in body
        assert "applyQueryOverrides();" in body
        assert "handoff-local-" not in body
        assert "localPocOpeningPayload" not in body
        assert "formPayload()" in body
        assert "<th>时间</th>" in body
        assert "function callTimeLabel(call)" in body
        assert "<th>录音</th>" in body
        assert "function recordingCell(call)" in body
        assert "function hasActiveRecordingPlayback()" in body
        assert "preservePlayback && hasActiveRecordingPlayback()" in body
        assert '`/calls/${encodeURIComponent(call.call_id)}/recording`' in body
        assert "<audio controls preload=\"metadata\"" in body
        assert 'target="_blank"' in body
        assert 'download="${escapeHtml(recordingFileName(call))}"' in body
        assert "window.setInterval(() => refreshCalls({ preservePlayback: true }), 2500);" in body
        assert 'id="identityName"' in body
        assert 'name="identityName"' in body
        assert 'value="项目员工"' in body
        assert 'id="employeeName"' not in body
        assert 'name="employeeName"' not in body
        assert 'id="personaId"' not in body
        assert 'name="personaId"' not in body
        assert 'id="debtId"' in body
        assert 'name="debtId"' in body
        assert 'value="2056600544053252097"' in body
        assert 'id="tenantId"' in body
        assert 'name="tenantId"' in body
        assert 'value="000000"' in body
        assert "启用手工开场白" not in body
        assert "开场白音色" not in body
        assert "业主姓名" not in body
        assert "待缴金额" not in body
        assert 'id="openingEnabled"' not in body
        assert 'id="openingVoice"' not in body
        assert 'id="ownerName"' not in body
        assert 'id="arrearsAmount"' not in body
        assert "const context = {};" in body
        assert (
            'for (const key of ["callId", "taskId", "identityName", "debtId", "tenantId"])'
            in body
        )
        assert "payload.context = context;" in body
        assert 'payload.opening = {' not in body
        assert 'delete payload.opening_enabled' not in body
        assert "ready 表示页面接口可用" not in body
        assert "外呼测试" in body
        assert "浏览器对话" in body
        assert "交接文档" in body
        assert "学习笔记" in body
        assert "Mac 接入指导" in body
        assert "推荐AGENT.md" in body
        assert 'href="/outbound-test"' in body
        assert 'href="/browser-realtime-test"' in body
        assert 'nav class="nav" aria-label="主导航"' in body
        assert "width: min(1360px, calc(100% - 24px))" in body
        assert "width: min(100% - 16px, 1360px)" in body
        assert body.index("刷新状态") > body.index('<main class="shell">')
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
        assert body.count('href="/docs/handoff"') == 1
        assert body.count('href="/browser-realtime-test"') == 1
        assert body.count('href="/docs/notes"') == 1
        assert body.count('href="/docs/mac-softphone"') == 1
        assert body.count('href="/docs/agent-readme"') == 1
        assert "项目文档" not in body
        assert "文档入口" not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


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
        assert "/browser-test-prompts/defaults" in body
        assert "/browser-test-prompts/database-preview" in body
        assert 'location.protocol === "file:" ? "http://127.0.0.1:19100" : ""' in body
        assert 'fetch(apiUrl("/browser-test-prompts/defaults")' in body
        assert 'fetch(apiUrl("/ready")' in body
        assert "mediaWebSocketUrl" in body
        assert 'if (location.protocol === "https:")' in body
        assert 'return `wss://${location.host}/media/${callId}`;' in body
        assert "loadDatabaseSpeakingStyle" in body
        assert "ws://127.0.0.1:9101/media/" in body
        assert "最终 Prompt 预览" in body
        assert "浏览器会话 ID" in body
        assert "数据库通话记录 callId（可选）" in body
        assert "debtId（必填）" in body
        assert 'id="debtId" value="2058923748267257858"' in body
        assert "<select id=\"identityName\">" in body
        assert '<option value="项目员工" selected>项目员工</option>' in body
        assert '<option value="企业客服">企业客服</option>' in body
        assert '<option value="企业法务">企业法务</option>' in body
        assert '<option value="律师">律师</option>' in body
        assert "第三方律师" not in body
        assert '<input id="identityName"' not in body
        assert "通话中禁止说金额" in body
        assert "非电话链路验证" in body
        assert 'id="personaId"' not in body
        assert "程序默认公共约束" in body
        assert "用户画像" in body
        assert "画像类型" in body
        assert 'id="personaProfile"' in body
        assert 'id="personaTypeValue"' in body
        assert 'id="personaProfileText"' in body
        assert "来自数据库 persona 策略" in body
        assert "只发送改动过的约束" in body
        assert 'class="constraint-field wide speaking-style-field"' in body
        assert "用于配置模型对话风格" in body
        assert "你说话偏向林黛玉" in body
        assert "你口吻拽拽的" in body
        assert ".speaking-style-field textarea" in body
        assert "min-height: 86px" in body
        assert 'class="constraint-grid"' in body
        assert 'class="constraint-field tall"' in body
        assert 'class="constraint-field wide"' in body
        assert 'data-section="communication_norms"' in body
        assert "沟通规范" in body
        assert '"communication_norms"' in body
        assert "let registeringPrompt = false;" in body
        assert 'if (registeringPrompt) return;' in body
        assert 'registerButton.disabled = true;' in body
        assert 'registerButton.textContent = "创建中...";' in body
        assert "playbackWorkletUrl" in body
        assert 'registerProcessor("playback-processor"' in body
        assert 'new AudioWorkletNode(audioContext, "playback-processor",' in body
        assert "createBufferSource" not in body
        assert 'class="check-line"' in body
        assert 'href="/outbound-test"' in body
        assert 'class="active" href="/browser-realtime-test" aria-current="page"' in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_webrtc_agent_test_page_is_served():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/webrtc-agent-test", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "WebRTC 坐席接入测试" in body
        assert "JsSIP" in body
        assert 'id="wsUrl" autocomplete="off"' in body
        assert "stun:stun.l.google.com:19302,stun:stun.cloudflare.com:3478" in body
        assert 'id="sipUri" value="sip:1001@111.229.146.182"' in body
        assert 'id="password" type="password" value="tenlocal1000"' in body
        assert 'id="targetUri" value="sip:9196@111.229.146.182"' in body
        assert 'src="/vendor/jssip.min.js"' in body
        assert "检查麦克风" in body
        assert "上线注册" in body
        assert "拨打测试" in body
        assert "呼叫本座席" in body
        assert "接听来电" in body
        assert 'id="incomingCallBanner"' in body
        assert "正在呼入" in body
        assert 'answerButton.classList.toggle("attention", incomingCall);' in body
        assert "拒接" in body
        assert "挂断" in body
        assert "待接通话" in body
        assert "接听选中通话" in body
        assert 'id="claimHandoffHint"' in body
        assert "function handoffClaimDisabledReason" in body
        assert "nextAutoSelectHandoffCallId" in body
        assert "已自动选中待接通话" in body
        assert "/webrtc-agent-test/call" in body
        assert "/calls?status=active&limit=50" in body
        assert "/handoff/claim" in body
        assert "async function refreshHandoffCalls(options = {})" in body
        assert "renderHandoffList({ preserveSelected });" in body
        assert "await refreshHandoffCalls({ preserveSelected: true });" in body
        assert "preserveSelected && selectedHandoffCall" in body
        assert "const selectedStillWaiting =" in body
        assert "handoff.can_claim" in body
        assert "renderHandoffTimeline(null);" in body
        assert "function defaultWebSocketUrl()" in body
        assert 'const protocol = secure ? "wss" : "ws";' in body
        assert 'const port = secure ? "7443" : "5066";' in body
        assert 'els.wsUrl.value = defaultWebSocketUrl();' in body
        assert "function buildAudioMediaConstraints()" in body
        assert "echoCancellation: true" in body
        assert "noiseSuppression: true" in body
        assert "autoGainControl: true" in body
        assert "mediaConstraints: buildAudioMediaConstraints()" in body
        assert "mediaConstraints: { audio: true, video: false }" not in body
        assert "new JsSIP.WebSocketInterface" in body
        assert "new JsSIP.UA" in body
        assert "register: true" in body
        assert 'event.originator === "remote"' in body
        assert "answerIncoming" in body
        assert "pendingIncomingSession.answer" in body
        assert "return true;" in body
        assert "return false;" in body
        assert "const microphoneReady = await checkMicrophone();" in body
        assert 'throw new Error("麦克风不可用，无法上线注册");' in body
        assert "mediaStream: micStream" in body
        assert "rtcAnswerConstraints" in body
        assert "boundSessions" in body
        assert "pcConfig: buildPeerConnectionConfig()" in body
        assert "iceServers" in body
        assert "navigator.mediaDevices.getUserMedia" in body
        assert 'href="/outbound-test"' in body
        assert 'href="/browser-realtime-test"' in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_webrtc_agent_call_endpoint_invokes_requester():
    captured_payloads = []

    def request_agent_call(payload):
        captured_payloads.append(payload)
        return {
            "agent_uuid": "agent-uuid-1",
            "agent_extension": "1001",
            "freeswitch_reply": "+OK agent call accepted",
        }

    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(
        config,
        webrtc_agent_call_requester=request_agent_call,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/webrtc-agent-test/call",
            data=json.dumps(
                {"agent_extension": "1001", "timeout_seconds": 12}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 202
        assert payload == {
            "status": "accepted",
            "agent_uuid": "agent-uuid-1",
            "agent_extension": "1001",
            "freeswitch_reply": "+OK agent call accepted",
        }
        assert captured_payloads == [
            {"agent_extension": "1001", "timeout_seconds": 12}
        ]
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_calls_endpoint_filters_active_calls():
    manager = FakeCallManager()
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/calls?status=active", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload == {
            "status": "ok",
            "calls": [
                {"call_id": "call-1", "status": "queued"},
                {"call_id": "call-2", "status": "waiting_agent"},
            ],
        }
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_handoff_endpoint_invokes_call_manager():
    manager = FakeCallManager()
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls/call-1/handoff",
            data=json.dumps(
                {
                    "agent_extension": "1001",
                    "trigger": "customer_requested",
                    "reason": "request_human",
                    "last_utterance": "我要转人工",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 202
        assert payload == {
            "status": "accepted",
            "call": {
                "call_id": "call-1",
                "status": "waiting_agent",
                "handoff": {
                    "state": "waiting_agent",
                    "last_utterance": "我要转人工",
                },
            },
        }
        assert manager.handoff_request == (
            "call-1",
            {
                "agent_extension": "1001",
                "trigger": "customer_requested",
                "reason": "request_human",
                "last_utterance": "我要转人工",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_handoff_claim_endpoint_invokes_call_manager():
    manager = FakeCallManager()
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls/call-1/handoff/claim",
            data=json.dumps(
                {
                    "agent_extension": "1001",
                    "agent_uuid": "agent-uuid-1",
                    "claimed_by": "agent-1001",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 202
        assert payload == {
            "status": "accepted",
            "call": {
                "call_id": "call-1",
                "status": "human_active",
                "handoff": {
                    "state": "human_active",
                    "agent_extension": "1001",
                    "claimed_by": "agent-1001",
                },
            },
        }
        assert manager.handoff_claim == (
            "call-1",
            {
                "agent_extension": "1001",
                "agent_uuid": "agent-uuid-1",
                "claimed_by": "agent-1001",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_handoff_transcript_endpoint_invokes_call_manager():
    manager = FakeCallManager()
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        request = Request(
            f"http://{host}:{port}/calls/call-1/handoff/transcript",
            data=json.dumps(
                {
                    "turns": [
                        {
                            "role": "assistant",
                            "speaker_type": "human_agent",
                            "agent_id": "agent-1001",
                            "text": "您好，我是物业客服。",
                        }
                    ]
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 202
        assert payload == {
            "status": "accepted",
            "call": {
                "call_id": "call-1",
                "status": "completed",
                "handoff": {"human_transcript_status": "completed"},
            },
        }
        assert manager.handoff_transcript == (
            "call-1",
            {
                "turns": [
                    {
                        "role": "assistant",
                        "speaker_type": "human_agent",
                        "agent_id": "agent-1001",
                        "text": "您好，我是物业客服。",
                    }
                ]
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_jssip_vendor_asset_is_served():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(f"http://{host}:{port}/vendor/jssip.min.js", timeout=3) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("application/javascript")
        assert "JsSIP" in body
        assert "WebSocketInterface" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_browser_test_prompt_defaults_endpoint_exposes_current_program_rules():
    config = GatewayConfig(server=ServerConfig(host="127.0.0.1", port=0))
    server = HealthServer(config, call_manager=FakeCallManager())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.address
        with urlopen(
            f"http://{host}:{port}/browser-test-prompts/defaults",
            timeout=3,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "ok"
        assert "电话客服口吻" in payload["speaking_style"]
        assert "数据库催收策略决定业务目标、推进方向和可表达的信息范围" in payload["sections"]["dialog_style"]
        assert "要求勿扰后必须礼貌结束" in payload["sections"]["critical_runtime"]
        assert "用户主动询问欠款金额" in payload["sections"]["amount_dispute"]
        assert "只围绕逾期费用提醒" in payload["sections"]["communication_norms"]
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_browser_test_prompts_endpoint_registers_prompt_snapshot_with_preview():
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
                    "mode": "manual",
                    "employee_name": "测试员工",
                    "identityName": "项目员工",
                    "strategy_core": "先确认本人。",
                    "debt_amount": "12.34",
                    "debtor_name": "金阳",
                    "debtor_gender": "女",
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
        assert "HTTP 测试规则。" in payload["prompt"]["preview"]
        assert payload["sensitive_summary"]["amount_in_prompt"] is False
        assert payload["sensitive_summary"]["amount_disclosure_forbidden"] is True
        snapshot = prompt_store.get("browser-http-test")
        assert snapshot is not None
        assert "HTTP 测试规则。" in snapshot.instructions
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_browser_test_prompt_database_preview_returns_database_speaking_style():
    class Preparer:
        def __init__(self):
            self.contexts = []

        def prepare(self, context):
            self.contexts.append(context)
            return BusinessPromptPreparation(
                prompt_snapshot=PromptSnapshot(
                    scene="企业法务:4",
                    version="postgres",
                    instructions=(
                        "# 客服语气配置\n数据库法务语气。\n"
                        "具体金额不写入本轮对话提示词；"
                        "无论身份是否确认，均不得在通话中说出具体金额。"
                    ),
                    content_hash="database-hash",
                    loaded_at_ms=123,
                    metadata={
                        "source": "postgres",
                        "identityName": "企业法务",
                        "personaId": "4",
                        "debtId": "2058923748267257858",
                        "strategy_core": "画像策略：强沟通意愿，先确认身份再推进。",
                        "speaking_style": "数据库法务语气。",
                    },
                ),
                opening=None,
            )

    preparer = Preparer()
    prompt_store = BrowserPromptTestStore(business_prompt_preparer=preparer)
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
            f"http://{host}:{port}/browser-test-prompts/database-preview",
            data=json.dumps(
                {
                    "context": {
                        "identityName": "企业法务",
                        "debtId": "2058923748267257858",
                    }
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "ok"
        assert payload["speaking_style"] == "数据库法务语气。"
        assert payload["personaId"] == "4"
        assert payload["persona_type"] == "习惯性拖延/博弈型"
        assert payload["identityName"] == "企业法务"
        assert payload["debtId"] == "2058923748267257858"
        assert payload["persona_profile"] == "画像策略：强沟通意愿，先确认身份再推进。"
        assert payload["prompt"]["content_hash"] == "database-hash"
        assert payload["sensitive_summary"]["amount_in_prompt"] is False
        assert payload["sensitive_summary"]["amount_disclosure_forbidden"] is True
        assert preparer.contexts == [
            {"identityName": "企业法务", "debtId": "2058923748267257858"}
        ]
        assert prompt_store.get("browser-preview") is None
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
            data=json.dumps({"call_id": "real-call-id", "mode": "manual"}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as err:
            payload = json.loads(err.read().decode("utf-8"))
            assert err.code == 400
            assert payload["status"] == "error"
            assert "browser-" in payload["error"]
        else:
            raise AssertionError("expected non-browser call id to be rejected")
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
        assert "浏览器对话" in body
        assert "交接文档" in body
        assert "学习笔记" in body
        assert "Mac 接入指导" in body
        assert "推荐AGENT.md" in body
        assert 'href="/outbound-test"' in body
        assert 'href="/browser-realtime-test"' in body
        assert 'nav class="nav" aria-label="主导航"' in body
        assert "width: min(1360px, calc(100% - 24px))" in body
        assert "width: min(100% - 16px, 1360px)" in body
        assert body.index("刷新状态") > body.index('<main class="shell">')
        assert 'href="/docs/handoff"' in body
        assert 'href="/docs/notes"' in body
        assert 'href="/docs/mac-softphone"' in body
        assert 'href="/docs/agent-readme"' in body
        assert body.count('href="/docs/handoff"') == 1
        assert body.count('href="/browser-realtime-test"') == 1
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
        assert 'href="/browser-realtime-test"' in body
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
        assert 'href="/browser-realtime-test"' in body
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
        assert 'href="/browser-realtime-test"' in body
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
        assert 'href="/browser-realtime-test"' in body
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
        assert 'href="/browser-realtime-test"' in body
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
        with urlopen(
            f"http://{host}:{port}/browser-realtime-test",
            timeout=3,
        ) as response:
            browser = response.read().decode("utf-8")

        assert _topbar_without_current_page(outbound) == _topbar_without_current_page(
            handoff
        )
        assert _topbar_without_current_page(handoff) == _topbar_without_current_page(
            notes
        )
        assert _topbar_without_current_page(notes) == _topbar_without_current_page(mac)
        assert _topbar_without_current_page(mac) == _topbar_without_current_page(agent)
        assert _topbar_without_current_page(agent) == _topbar_without_current_page(
            browser
        )
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
        self.handoff_request = None
        self.handoff_claim = None
        self.handoff_transcript = None
        self.calls = [
            {"call_id": "call-1", "status": "queued"},
            {"call_id": "call-2", "status": "waiting_agent"},
            {"call_id": "call-3", "status": "completed"},
        ]

    def create_call(self, payload):
        self.created_payload = payload
        return {"call_id": "call-1", "status": "queued"}

    def list_calls(self, *, limit=50):
        return self.calls[:limit]

    def get_call(self, call_id):
        return next((call for call in self.calls if call.get("call_id") == call_id), None)

    def request_hangup(self, call_id, *, cause="NORMAL_CLEARING"):
        return {"call_id": call_id, "status": "hangup_requested", "cause": cause}

    def request_handoff(self, call_id, payload):
        self.handoff_request = (call_id, payload)
        return {
            "call_id": call_id,
            "status": "waiting_agent",
            "handoff": {
                "state": "waiting_agent",
                "last_utterance": payload.get("last_utterance"),
            },
        }

    def claim_handoff(self, call_id, payload):
        self.handoff_claim = (call_id, payload)
        return {
            "call_id": call_id,
            "status": "human_active",
            "handoff": {
                "state": "human_active",
                "agent_extension": payload.get("agent_extension"),
                "claimed_by": payload.get("claimed_by"),
            },
        }

    def complete_handoff_transcript(self, call_id, payload):
        self.handoff_transcript = (call_id, payload)
        return {
            "call_id": call_id,
            "status": "completed",
            "handoff": {"human_transcript_status": "completed"},
        }
