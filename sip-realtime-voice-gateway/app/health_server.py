from __future__ import annotations

import json
import logging
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import GatewayConfig

LOGGER = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, config: GatewayConfig):
        self.config = config
        handler = self._make_handler(config)
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
    def _make_handler(config: GatewayConfig) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            server_version = "SipRealtimeVoiceGateway/0.1"

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "service": "sip-realtime-voice-gateway",
                        },
                    )
                    return

                if self.path == "/ready":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": "ready",
                            "config": {
                                "server": asdict(config.server),
                                "freeswitch": asdict(config.freeswitch),
                                "realtime": {
                                    "provider": config.realtime.provider,
                                    "model": config.realtime.model,
                                    "voice": config.realtime.voice,
                                },
                                "features": asdict(config.features),
                            },
                        },
                    )
                    return

                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "not_found", "path": self.path},
                )

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.info("http %s", format % args)

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

        return Handler

