from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.handoff_transcript import (
    HttpHumanHandoffTranscriptProcessor,
    MockHumanHandoffTranscriptProcessor,
)


def test_http_human_handoff_transcript_processor_posts_recording_paths():
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {
                    "turns": [
                        {
                            "role": "assistant",
                            "speaker_type": "human_agent",
                            "agent_id": "agent-1001",
                            "text": "您好，我是物业客服。",
                            "start_ms": 1200,
                            "end_ms": 2600,
                            "confidence": 0.92,
                        },
                        {
                            "role": "user",
                            "speaker_type": "customer",
                            "text": "我想确认一下费用。",
                        },
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        url = f"http://{server.server_address[0]}:{server.server_address[1]}/asr"
        processor = HttpHumanHandoffTranscriptProcessor(url, timeout_seconds=2.0)
        turns = processor.process(
            {
                "call_id": "call-1",
                "context": {"callId": "business-1"},
                "agent_id": "agent-1001",
                "agent_uuid": "agent-uuid-1",
                "customer_recording_path": "/tmp/call-1-customer.wav",
                "agent_recording_path": "/tmp/call-1-agent.wav",
            }
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert captured["path"] == "/asr"
    assert captured["body"] == {
        "call_id": "call-1",
        "context": {"callId": "business-1"},
        "agent_id": "agent-1001",
        "agent_uuid": "agent-uuid-1",
        "customer_recording_path": "/tmp/call-1-customer.wav",
        "agent_recording_path": "/tmp/call-1-agent.wav",
    }
    assert turns == [
        {
            "role": "assistant",
            "speaker_type": "human_agent",
            "agent_id": "agent-1001",
            "text": "您好，我是物业客服。",
            "start_ms": 1200,
            "end_ms": 2600,
            "confidence": 0.92,
        },
        {
            "role": "user",
            "speaker_type": "customer",
            "text": "我想确认一下费用。",
        },
    ]


def test_mock_human_handoff_transcript_processor_returns_human_agent_turn():
    processor = MockHumanHandoffTranscriptProcessor()

    turns = processor.process(
        {
            "agent_id": "agent-1001",
            "customer_recording_path": "/tmp/call-1-customer.wav",
            "agent_recording_path": "/tmp/call-1-agent.wav",
        }
    )

    assert turns == [
        {
            "role": "assistant",
            "speaker_type": "human_agent",
            "agent_id": "agent-1001",
            "text": "人工坐席已接入并完成通话。",
        }
    ]
