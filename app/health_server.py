from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .browser_prompt_test import (
    BrowserPromptDatabasePreview,
    BrowserPromptRegistration,
    BrowserPromptTestStore,
    browser_public_constraint_defaults,
)
from .call_control import (
    CallControlError,
    OutboundCallManager,
    originate_webrtc_agent_test_call,
)
from .config import GatewayConfig

LOGGER = logging.getLogger(__name__)
AgentCallRequester = Callable[[dict[str, Any]], dict[str, Any]]

DOCS = {
    "handoff": {
        "title": "SIP 实时语音网关交接总文档",
        "html_path": "handoff.html",
        "description": "当前业务链路、已实现能力、测试方式、数据格式和后续边界。",
    },
    "notes": {
        "title": "TEN 电话线路接入学习笔记",
        "html_path": "notes.html",
        "description": "历史调研、关键概念、本地测试和实时语音链路学习记录。",
    },
    "mac-softphone": {
        "title": "Mac 软电话接入 9199 本地测试指导",
        "html_path": "mac-softphone.html",
        "description": "macOS 软电话注册、拨入、外呼验证和常见问题。",
    },
    "agent-readme": {
        "title": "推荐 AGENT.md 内容",
        "html_path": "agent-readme.html",
        "description": "推荐的 AI / Agent 协作方式、分析方式、执行方式、文档和 Git 要求。",
    },
}

PERSONA_TYPE_BY_ID = {
    "1": "疏忽遗忘型",
    "2": "暂时困难型",
    "3": "投诉挂钩型",
    "4": "习惯性拖延/博弈型",
    "5": "房屋空置型",
    "6": "产权纠纷型",
    "7": "租赁推诿型",
    "8": "历史遗留问题型",
    "9": "信息失联型",
    "10": "恶意对抗型",
}


class HealthServer:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        call_manager: OutboundCallManager | None = None,
        browser_prompt_store: BrowserPromptTestStore | None = None,
        webrtc_agent_call_requester: AgentCallRequester | None = None,
    ):
        self.config = config
        self.call_manager = call_manager
        self.browser_prompt_store = browser_prompt_store
        handler = self._make_handler(
            config,
            call_manager=call_manager,
            browser_prompt_store=browser_prompt_store,
            webrtc_agent_call_requester=webrtc_agent_call_requester,
        )
        self._server = ThreadingHTTPServer(
            (config.server.host, config.server.port),
            handler,
        )

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address
        return str(host), int(port)

    def serve_forever(self) -> None:
        host, port = self.address
        LOGGER.info("health server listening host=%s port=%s", host, port)
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @staticmethod
    def _make_handler(
        config: GatewayConfig,
        *,
        call_manager: OutboundCallManager | None = None,
        browser_prompt_store: BrowserPromptTestStore | None = None,
        webrtc_agent_call_requester: AgentCallRequester | None = None,
    ) -> type[BaseHTTPRequestHandler]:
        agent_call_requester = webrtc_agent_call_requester or (
            lambda payload: originate_webrtc_agent_test_call(config, payload)
        )

        class Handler(BaseHTTPRequestHandler):
            server_version = "SipRealtimeVoiceGateway/0.1"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "service": "sip-realtime-voice-gateway",
                        },
                    )
                    return

                if parsed.path == "/ready":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ready",
                            "config": {
                                "server": asdict(config.server),
                                "freeswitch": asdict(config.freeswitch),
                                "realtime": {
                                    "provider": "doubao_s2s",
                                    "resource_id": config.doubao_s2s.resource_id,
                                    "speaker": config.doubao_s2s.speaker,
                                    "output_sample_rate": (
                                        config.doubao_s2s.output_sample_rate
                                    ),
                                },
                                "features": asdict(config.features),
                                "call_recording": asdict(config.call_recording),
                                "human_transcript": asdict(
                                    config.human_transcript
                                ),
                                "handoff": asdict(config.handoff),
                                "flow_callback": asdict(config.flow_callback),
                                "rocketmq": asdict(config.rocketmq),
                                "outbound": {
                                    "enabled": config.outbound.enabled,
                                    "endpoint_template": (
                                        config.outbound.endpoint_template
                                    ),
                                    "dialplan_extension": (
                                        config.outbound.dialplan_extension
                                    ),
                                    "dialplan_context": (
                                        config.outbound.dialplan_context
                                    ),
                                    "event_socket_enabled": (
                                        config.event_socket.enabled
                                    ),
                                },
                            },
                        },
                    )
                    return

                if parsed.path in {"/", "/outbound-test"}:
                    self._send_html(HTTPStatus.OK, _load_outbound_test_html())
                    return

                if parsed.path == "/browser-realtime-test":
                    self._send_html(HTTPStatus.OK, _load_browser_realtime_test_html())
                    return

                if parsed.path == "/webrtc-agent-test":
                    self._send_html(HTTPStatus.OK, _load_webrtc_agent_test_html())
                    return

                if parsed.path == "/vendor/jssip.min.js":
                    self._send_asset(
                        HTTPStatus.OK,
                        _load_vendor_asset("jssip.min.js"),
                        "application/javascript; charset=utf-8",
                    )
                    return

                if parsed.path == "/browser-test-prompts/defaults":
                    defaults = (
                        browser_prompt_store.public_constraint_defaults()
                        if browser_prompt_store is not None
                        else browser_public_constraint_defaults()
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": "ok", **defaults},
                    )
                    return

                if parsed.path == "/docs":
                    self._send_redirect("/docs/handoff")
                    return

                doc_id = _doc_id_from_path(parsed.path)
                if doc_id is not None:
                    try:
                        self._send_html(HTTPStatus.OK, _load_doc_html(doc_id))
                    except KeyError:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"status": "not_found", "doc_id": doc_id},
                        )
                    return

                call_id = _recording_call_id_from_path(parsed.path)
                if call_id is not None:
                    self._send_call_recording(call_id, include_body=True)
                    return

                if parsed.path == "/calls":
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    limit = _query_int(parsed.query, "limit", default=50)
                    status_filter = _query_str(parsed.query, "status")
                    calls = call_manager.list_calls(limit=limit)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "calls": _filter_calls_by_status(calls, status_filter),
                        },
                    )
                    return

                call_id = _call_id_from_path(parsed.path)
                if call_id is not None:
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    call = call_manager.get_call(call_id)
                    if call is None:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"status": "not_found", "call_id": call_id},
                        )
                        return
                    self._send_json(HTTPStatus.OK, {"status": "ok", "call": call})
                    return

                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_found", "path": parsed.path},
                )

            def do_HEAD(self) -> None:
                parsed = urlparse(self.path)
                call_id = _recording_call_id_from_path(parsed.path)
                if call_id is not None:
                    self._send_call_recording(call_id, include_body=False)
                    return

                self.send_response(HTTPStatus.NOT_FOUND.value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/browser-test-prompts/database-preview":
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
                        preview = browser_prompt_store.preview_database(
                            self._read_json_body()
                        )
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
                        _browser_prompt_database_preview_payload(preview),
                    )
                    return

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
                        registration = browser_prompt_store.register(
                            self._read_json_body()
                        )
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
                        _browser_prompt_registration_payload(registration),
                    )
                    return

                if parsed.path == "/webrtc-agent-test/call":
                    try:
                        result = agent_call_requester(self._read_json_body())
                    except CallControlError as err:
                        self._send_json(
                            HTTPStatus(err.status_code),
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
                        HTTPStatus.ACCEPTED,
                        {"status": "accepted", **result},
                    )
                    return

                if parsed.path == "/calls":
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    try:
                        call = call_manager.create_call(self._read_json_body())
                    except CallControlError as err:
                        self._send_json(
                            HTTPStatus(err.status_code),
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
                        HTTPStatus.ACCEPTED,
                        {
                            "status": "accepted",
                            "accepted": True,
                            "businessId": (
                                call.get("external_call_id") or call.get("call_id")
                            ),
                            "message": "AI外呼任务已受理",
                            "call": call,
                        },
                    )
                    return

                call_id = _handoff_transcript_call_id_from_path(parsed.path)
                if call_id is not None:
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    try:
                        call = call_manager.complete_handoff_transcript(
                            call_id,
                            self._read_json_body(),
                        )
                    except CallControlError as err:
                        self._send_json(
                            HTTPStatus(err.status_code),
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
                        HTTPStatus.ACCEPTED,
                        {"status": "accepted", "call": call},
                    )
                    return

                call_id = _handoff_claim_call_id_from_path(parsed.path)
                if call_id is not None:
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    try:
                        call = call_manager.claim_handoff(
                            call_id,
                            self._read_json_body(),
                        )
                    except CallControlError as err:
                        self._send_json(
                            HTTPStatus(err.status_code),
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
                        HTTPStatus.ACCEPTED,
                        {"status": "accepted", "call": call},
                    )
                    return

                call_id = _handoff_call_id_from_path(parsed.path)
                if call_id is not None:
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    try:
                        call = call_manager.request_handoff(
                            call_id,
                            self._read_json_body(),
                        )
                    except CallControlError as err:
                        self._send_json(
                            HTTPStatus(err.status_code),
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
                        HTTPStatus.ACCEPTED,
                        {"status": "accepted", "call": call},
                    )
                    return

                call_id = _hangup_call_id_from_path(parsed.path)
                if call_id is not None:
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    try:
                        body = self._read_optional_json_body()
                    except json.JSONDecodeError:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"status": "error", "error": "invalid JSON body"},
                        )
                        return
                    cause = "NORMAL_CLEARING"
                    if isinstance(body, dict) and body.get("cause"):
                        cause = str(body["cause"])
                    try:
                        call = call_manager.request_hangup(call_id, cause=cause)
                    except CallControlError as err:
                        self._send_json(
                            HTTPStatus(err.status_code),
                            {"status": "error", "error": str(err)},
                        )
                        return
                    self._send_json(
                        HTTPStatus.ACCEPTED,
                        {"status": "accepted", "call": call},
                    )
                    return

                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_found", "path": parsed.path},
                )

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.info("http %s", format % args)

            def _read_json_body(self) -> dict[str, Any]:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    return {}
                if content_length > 65536:
                    raise CallControlError("request body is too large")
                raw = self.rfile.read(content_length)
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise CallControlError("request body must be a JSON object")
                return payload

            def _read_optional_json_body(self) -> dict[str, Any] | None:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    return None
                raw = self.rfile.read(content_length)
                payload = json.loads(raw.decode("utf-8"))
                return payload if isinstance(payload, dict) else None

            def _send_call_recording(
                self,
                call_id: str,
                *,
                include_body: bool,
            ) -> None:
                if call_manager is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"status": "unavailable", "error": "call control disabled"},
                    )
                    return
                call = call_manager.get_call(call_id)
                if call is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"status": "error", "error": "call not found"},
                    )
                    return
                try:
                    recording_path = _call_recording_file_path(config, call)
                    self._send_audio_file(recording_path, include_body=include_body)
                except CallControlError as err:
                    self._send_json(
                        HTTPStatus(err.status_code),
                        {"status": "error", "error": str(err)},
                    )
                    return
                except OSError:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"status": "error", "error": "recording file not found"},
                    )
                    return

            def _send_json(
                self,
                status: HTTPStatus,
                payload: dict[str, Any],
            ) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, status: HTTPStatus, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_asset(
                self,
                status: HTTPStatus,
                body: bytes,
                content_type: str,
            ) -> None:
                self.send_response(status.value)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_audio_file(self, path: Path, *, include_body: bool = True) -> None:
                file_size = path.stat().st_size
                byte_range = _parse_byte_range(
                    self.headers.get("Range"),
                    file_size=file_size,
                )
                if byte_range == "invalid":
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.value)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if byte_range is None:
                    start = 0
                    end = file_size - 1
                    status = HTTPStatus.OK
                else:
                    start, end = byte_range
                    status = HTTPStatus.PARTIAL_CONTENT

                length = end - start + 1
                self.send_response(status.value)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Accept-Ranges", "bytes")
                if status == HTTPStatus.PARTIAL_CONTENT:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if include_body:
                    _copy_file_range(path, self.wfile.write, start=start, length=length)

            def _send_redirect(self, location: str) -> None:
                self.send_response(HTTPStatus.FOUND.value)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


def _call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    if not path.startswith(prefix):
        return None
    suffix = path[len(prefix) :].strip("/")
    if not suffix or "/" in suffix:
        return None
    return suffix


def _recording_call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    suffix = "/recording"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    call_id = path[len(prefix) : -len(suffix)].strip("/")
    if not call_id or "/" in call_id:
        return None
    return call_id


def _query_str(query: str, name: str) -> str | None:
    values = parse_qs(query).get(name)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _filter_calls_by_status(
    calls: list[dict[str, Any]],
    status_filter: str | None,
) -> list[dict[str, Any]]:
    if status_filter is None:
        return calls
    if status_filter == "active":
        terminal_statuses = {
            "completed",
            "failed",
            "busy",
            "no_answer",
            "canceled",
            "hangup_failed",
        }
        return [
            call
            for call in calls
            if str(call.get("status")) not in terminal_statuses
        ]
    return [
        call
        for call in calls
        if str(call.get("status")) == status_filter
    ]


def _hangup_call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    suffix = "/hangup"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    call_id = path[len(prefix) : -len(suffix)].strip("/")
    if not call_id or "/" in call_id:
        return None
    return call_id


def _handoff_call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    suffix = "/handoff"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    call_id = path[len(prefix) : -len(suffix)].strip("/")
    if not call_id or "/" in call_id:
        return None
    return call_id


def _handoff_claim_call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    suffix = "/handoff/claim"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    call_id = path[len(prefix) : -len(suffix)].strip("/")
    if not call_id or "/" in call_id:
        return None
    return call_id


def _handoff_transcript_call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    suffix = "/handoff/transcript"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    call_id = path[len(prefix) : -len(suffix)].strip("/")
    if not call_id or "/" in call_id:
        return None
    return call_id


def _doc_id_from_path(path: str) -> str | None:
    prefix = "/docs/"
    if not path.startswith(prefix):
        return None
    doc_id = path[len(prefix) :].strip("/")
    if not doc_id or "/" in doc_id:
        return None
    return doc_id


def _query_int(query: str, name: str, *, default: int) -> int:
    values = parse_qs(query).get(name)
    if not values:
        return default
    try:
        return max(1, min(int(values[0]), 500))
    except ValueError:
        return default


def _call_recording_file_path(config: GatewayConfig, call: dict[str, Any]) -> Path:
    recording_path = _payload_text(call.get("recording_path"))
    if not recording_path:
        raise CallControlError("recording path not found", status_code=404)

    recording_dir = config.call_recording.directory.rstrip("/")
    if recording_path == recording_dir:
        relative_path = ""
    elif recording_path.startswith(f"{recording_dir}/"):
        relative_path = recording_path[len(recording_dir) + 1 :]
    else:
        raise CallControlError(
            "recording path is outside call_recording.directory",
            status_code=400,
        )
    if not relative_path or Path(relative_path).suffix.lower() != ".wav":
        raise CallControlError("recording file must be a wav file", status_code=400)

    if config.call_recording.host_directory.strip():
        base_path = Path(config.call_recording.host_directory)
        file_path = base_path / relative_path
    else:
        base_path = Path(recording_dir)
        file_path = Path(recording_path)

    resolved_base = base_path.expanduser().resolve(strict=False)
    resolved_file = file_path.expanduser().resolve(strict=False)
    if not resolved_file.is_relative_to(resolved_base):
        raise CallControlError(
            "recording path is outside call_recording.directory",
            status_code=400,
        )
    if not resolved_file.is_file():
        raise CallControlError("recording file not found", status_code=404)
    return resolved_file


def _parse_byte_range(
    value: str | None,
    *,
    file_size: int,
) -> tuple[int, int] | str | None:
    if not value:
        return None
    if file_size <= 0:
        return "invalid"
    prefix = "bytes="
    if not value.startswith(prefix):
        return "invalid"
    range_spec = value[len(prefix) :].strip()
    if "," in range_spec or "-" not in range_spec:
        return "invalid"
    start_text, end_text = range_spec.split("-", 1)
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return "invalid"
            return max(file_size - suffix_length, 0), file_size - 1

        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    except ValueError:
        return "invalid"

    if start < 0 or start >= file_size or end < start:
        return "invalid"
    return start, min(end, file_size - 1)


def _copy_file_range(
    path: Path,
    write: Callable[[bytes], object],
    *,
    start: int,
    length: int,
) -> None:
    remaining = length
    with path.open("rb") as file:
        file.seek(start)
        while remaining > 0:
            chunk = file.read(min(remaining, 256 * 1024))
            if not chunk:
                break
            write(chunk)
            remaining -= len(chunk)


def _load_outbound_test_html() -> str:
    html_path = Path(__file__).resolve().parent.parent / "static" / "outbound-test.html"
    return html_path.read_text(encoding="utf-8")


def _load_browser_realtime_test_html() -> str:
    html_path = (
        Path(__file__).resolve().parent.parent
        / "static"
        / "browser-realtime-test.html"
    )
    return html_path.read_text(encoding="utf-8")


def _load_webrtc_agent_test_html() -> str:
    html_path = (
        Path(__file__).resolve().parent.parent
        / "static"
        / "webrtc-agent-test.html"
    )
    return html_path.read_text(encoding="utf-8")


def _load_vendor_asset(filename: str) -> bytes:
    asset_path = Path(__file__).resolve().parent.parent / "static" / "vendor" / filename
    return asset_path.read_bytes()


def _browser_prompt_registration_payload(
    registration: BrowserPromptRegistration,
) -> dict[str, Any]:
    snapshot = registration.prompt_snapshot
    persona_type = _persona_type_from_metadata(snapshot.metadata)
    return {
        "status": "ok",
        "call_id": registration.call_id,
        "mode": registration.mode,
        "persona_profile": snapshot.metadata.get("strategy_core") or "",
        "persona_type": persona_type,
        "prompt": {
            "scene": snapshot.scene,
            "version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "loaded_at_ms": snapshot.loaded_at_ms,
            "metadata": snapshot.metadata,
            "preview": snapshot.instructions,
        },
        "sensitive_summary": registration.sensitive_summary,
        "opening": registration.opening,
        "warnings": registration.warnings,
        "expires_in_seconds": registration.expires_in_seconds,
    }


def _browser_prompt_database_preview_payload(
    preview: BrowserPromptDatabasePreview,
) -> dict[str, Any]:
    snapshot = preview.prompt_snapshot
    metadata = snapshot.metadata
    persona_type = _persona_type_from_metadata(metadata)
    return {
        "status": "ok",
        "mode": "database",
        "identityName": metadata.get("identityName"),
        "personaId": metadata.get("personaId"),
        "debtId": metadata.get("debtId"),
        "persona_profile": metadata.get("strategy_core") or "",
        "persona_type": persona_type,
        "speaking_style": metadata.get("speaking_style") or "",
        "prompt": {
            "scene": snapshot.scene,
            "version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "loaded_at_ms": snapshot.loaded_at_ms,
            "metadata": metadata,
        },
        "sensitive_summary": preview.sensitive_summary,
        "opening": preview.opening,
    }


def _persona_type_from_metadata(metadata: dict[str, Any]) -> str:
    explicit_type = _payload_text(
        metadata.get("persona_type") or metadata.get("personaType")
    )
    if explicit_type:
        return explicit_type
    persona_id = _payload_text(metadata.get("personaId"))
    if persona_id:
        return PERSONA_TYPE_BY_ID.get(persona_id, "")
    return ""


def _payload_text(value: object) -> str:
    return str(value or "").strip()


def _load_doc_html(doc_id: str) -> str:
    metadata = DOCS[doc_id]
    doc_path = (
        Path(__file__).resolve().parent.parent
        / "static"
        / "pages"
        / metadata["html_path"]
    )
    return _document_shell(
        title=metadata["title"],
        body=doc_path.read_text(encoding="utf-8"),
        current_doc=doc_id,
    )


def _document_shell(
    *,
    title: str,
    body: str,
    current_doc: str | None = None,
) -> str:
    nav_items = [
        ("/outbound-test", "外呼测试", None),
        ("/browser-realtime-test", "浏览器对话", None),
        ("/docs/handoff", "交接文档", "handoff"),
        ("/docs/notes", "学习笔记", "notes"),
        ("/docs/mac-softphone", "Mac 接入指导", "mac-softphone"),
        ("/docs/agent-readme", "推荐AGENT.md", "agent-readme"),
    ]
    nav_links = []
    for href, label, doc_id in nav_items:
        active = doc_id == current_doc
        nav_links.append(
            '<a{class_name}{current} href="{href}">{label}</a>'.format(
                class_name=' class="active"' if active else "",
                current=' aria-current="page"' if active else "",
                href=html.escape(href),
                label=html.escape(label),
            )
        )
    if current_doc is not None:
        body, toc = _add_heading_anchors_and_toc(body)
        page_body = f'<div class="doc-layout">{toc}<article>{body}</article></div>'
    else:
        page_body = f"<article>{body}</article>"
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f4f6f8;
        --surface: #ffffff;
        --panel: #ffffff;
        --text: #151922;
        --muted: #667085;
        --line: #d9dee8;
        --green: #087443;
        --green-soft: #e8f5ee;
        --code: #101828;
        --shadow: 0 12px 28px rgba(21, 25, 34, 0.08);
        font-family: Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      }}
      * {{
        box-sizing: border-box;
      }}
      body {{
        margin: 0;
        background: var(--bg);
        color: var(--text);
      }}
      html {{
        scroll-behavior: smooth;
      }}
      .topbar {{
        position: sticky;
        top: 0;
        z-index: 20;
        border-bottom: 1px solid var(--line);
        background: rgba(244, 246, 248, 0.96);
        backdrop-filter: blur(10px);
      }}
      .topbar-inner {{
        width: min(1360px, calc(100% - 24px));
        height: 64px;
        margin: 0 auto;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
      }}
      .brand {{
        flex: 0 0 220px;
        min-width: 220px;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }}
      .brand strong {{
        font-size: 17px;
        line-height: 1.2;
      }}
      .brand span {{
        color: var(--muted);
        font-size: 12px;
        line-height: 1.2;
      }}
      .nav {{
        display: flex;
        align-items: center;
        gap: 8px;
        margin-left: auto;
      }}
      .nav a {{
        min-height: 36px;
        min-width: 96px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        color: var(--text);
        text-decoration: none;
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 0 12px;
        background: var(--surface);
        font-size: 13px;
        font-weight: 500;
        line-height: 1;
        white-space: nowrap;
      }}
      .nav a.active {{
        color: #075c44;
        border-color: #8ccfc0;
        background: var(--green-soft);
      }}
      .nav a:hover {{
        border-color: #9aa7bb;
      }}
      main {{
        width: min(1360px, calc(100% - 24px));
        margin: 0 auto;
        padding: 18px 0 48px;
      }}
      .doc-layout {{
        display: grid;
        grid-template-columns: 220px minmax(0, 1fr);
        gap: 14px;
        align-items: start;
      }}
      .doc-toc {{
        min-width: 0;
      }}
      .doc-toc-inner {{
        position: sticky;
        top: 82px;
        max-height: calc(100vh - 100px);
        overflow: auto;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 10px;
      }}
      .doc-toc-title {{
        display: block;
        margin: 0 0 10px;
        font-size: 14px;
      }}
      .doc-toc nav {{
        display: grid;
        gap: 4px;
      }}
      .doc-toc a {{
        display: block;
        border-radius: 6px;
        padding: 7px 8px;
        color: var(--muted);
        text-decoration: none;
        font-size: 13px;
        line-height: 1.4;
        overflow-wrap: anywhere;
      }}
      .doc-toc a:hover {{
        background: var(--green-soft);
        color: #075c44;
      }}
      .doc-toc a.active {{
        background: var(--green-soft);
        color: #075c44;
        font-weight: 650;
      }}
      article {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 28px;
        box-shadow: var(--shadow);
      }}
      h1 {{
        margin: 0 0 20px;
        font-size: 28px;
        line-height: 1.3;
      }}
      h1, h2, h3, h4 {{
        scroll-margin-top: 88px;
      }}
      h2 {{
        margin: 30px 0 12px;
        padding-top: 12px;
        border-top: 1px solid var(--line);
        font-size: 22px;
      }}
      h3 {{
        margin: 24px 0 10px;
        font-size: 18px;
      }}
      h4 {{
        margin: 20px 0 8px;
        font-size: 15px;
      }}
      p, li {{
        line-height: 1.72;
      }}
      p {{
        margin: 10px 0;
      }}
      ul, ol {{
        margin: 10px 0 14px 22px;
        padding: 0;
      }}
      code {{
        font-family: "Cascadia Mono", Consolas, monospace;
        font-size: 0.92em;
        color: #12463f;
        background: var(--green-soft);
        border-radius: 4px;
        padding: 1px 4px;
      }}
      pre {{
        overflow: auto;
        border-radius: 8px;
        border: 1px solid #253044;
        background: var(--code);
        color: #d1fadf;
        padding: 14px;
        line-height: 1.55;
      }}
      pre code {{
        color: inherit;
        background: transparent;
        padding: 0;
      }}
      pre.mermaid {{
        border-color: #b8c7dc;
        background: #f8fbff;
        color: #17324d;
      }}
      blockquote {{
        margin: 12px 0;
        border-left: 3px solid var(--green);
        padding: 4px 0 4px 14px;
        color: var(--muted);
        background: #fbfcfe;
      }}
      @media (max-width: 720px) {{
        .topbar-inner,
        main {{
          width: min(100% - 16px, 1360px);
        }}
        .topbar-inner {{
          height: auto;
          min-height: 76px;
          align-items: flex-start;
          flex-direction: column;
          padding: 10px 0;
        }}
        .brand {{
          flex: 0 1 auto;
          min-width: 0;
        }}
        .nav {{
          width: 100%;
          margin-left: 0;
          flex-wrap: wrap;
        }}
        .nav a {{
          flex: 1 1 calc(50% - 4px);
          min-width: 112px;
        }}
        main {{
          padding-top: 16px;
        }}
        .doc-layout {{
          grid-template-columns: 1fr;
        }}
        .doc-toc-inner {{
          position: static;
          max-height: 260px;
        }}
        article {{
          padding: 18px;
        }}
        h1 {{
          font-size: 23px;
        }}
      }}
    </style>
  </head>
  <body>
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand">
          <strong>SIP 实时语音网关</strong>
          <span>本地 9199 外呼控制台</span>
        </div>
        <nav class="nav" aria-label="主导航">{"".join(nav_links)}</nav>
      </div>
    </header>
    <main>{page_body}</main>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>
      if (window.mermaid) {{
        window.mermaid.initialize({{ startOnLoad: true, securityLevel: "strict" }});
      }}
      const tocLinks = Array.from(document.querySelectorAll(".doc-toc a"));
      const tocTargets = tocLinks
        .map((link) => document.querySelector(link.getAttribute("href")))
        .filter(Boolean);
      if (tocLinks.length && "IntersectionObserver" in window) {{
        const byId = new Map(tocLinks.map((link) => [link.hash.slice(1), link]));
        const observer = new IntersectionObserver(
          (entries) => {{
            const visible = entries
              .filter((entry) => entry.isIntersecting)
              .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
            if (!visible) return;
            tocLinks.forEach((link) => link.classList.remove("active"));
            const active = byId.get(visible.target.id);
            if (active) active.classList.add("active");
          }},
          {{ rootMargin: "-88px 0px -65% 0px", threshold: 0.01 }}
        );
        tocTargets.forEach((target) => observer.observe(target));
      }}
    </script>
  </body>
</html>"""


_HEADING_RE = re.compile(r"<h([1-4])>(.*?)</h\1>")


def _add_heading_anchors_and_toc(body: str) -> tuple[str, str]:
    toc_items: list[str] = []
    heading_index = 0

    def replace_heading(match: re.Match[str]) -> str:
        nonlocal heading_index
        heading_index += 1
        level = int(match.group(1))
        content = match.group(2)
        section_id = f"section-{heading_index}"
        label = _strip_html(content)
        if level == 2:
            toc_items.append(
                '<a href="#{section_id}">{label}</a>'.format(
                    section_id=section_id,
                    label=html.escape(label),
                )
            )
        return f'<h{level} id="{section_id}">{content}</h{level}>'

    anchored_body = _HEADING_RE.sub(replace_heading, body)
    toc = (
        '<aside class="doc-toc">'
        '<div class="doc-toc-inner">'
        '<strong class="doc-toc-title">目录</strong>'
        '<nav aria-label="文档目录">'
        + "".join(toc_items)
        + "</nav></div></aside>"
    )
    return anchored_body, toc


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value)
    return html.unescape(text).strip()
