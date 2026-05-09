from __future__ import annotations

import json
import threading
from urllib.request import urlopen

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

