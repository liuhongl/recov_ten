from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .call_control import CallControlError, OutboundCallManager
from .config import GatewayConfig

LOGGER = logging.getLogger(__name__)

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


class HealthServer:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        call_manager: OutboundCallManager | None = None,
    ):
        self.config = config
        self.call_manager = call_manager
        handler = self._make_handler(config, call_manager=call_manager)
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
    ) -> type[BaseHTTPRequestHandler]:
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

                if parsed.path == "/calls":
                    if call_manager is None:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "unavailable", "error": "call control disabled"},
                        )
                        return
                    limit = _query_int(parsed.query, "limit", default=50)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "calls": call_manager.list_calls(limit=limit),
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

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
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


def _hangup_call_id_from_path(path: str) -> str | None:
    prefix = "/calls/"
    suffix = "/hangup"
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


def _load_outbound_test_html() -> str:
    html_path = Path(__file__).resolve().parent.parent / "static" / "outbound-test.html"
    return html_path.read_text(encoding="utf-8")


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
