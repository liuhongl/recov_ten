from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app.handoff_asr_adapter import (
    DoubaoS2SAudioTranscriber,
    HandoffAsrAdapterError,
    HandoffAsrHttpHandler,
    HandoffAsrProcessor,
)
from app.doubao_s2s_client import DoubaoS2SCredentials, DoubaoS2SSessionConfig
from app.wav_io import write_pcm16_wav


def test_handoff_asr_processor_returns_standard_human_turns(tmp_path):
    customer_wav = tmp_path / "call-1-customer.wav"
    agent_wav = tmp_path / "call-1-agent.wav"
    write_pcm16_wav(customer_wav, b"\x01\x00\x02\x00", sample_rate=8000)
    write_pcm16_wav(agent_wav, b"\x03\x00\x04\x00", sample_rate=8000)
    transcriber = FakeTranscriber(
        {
            str(agent_wav): "您好，我是物业客服。",
            str(customer_wav): "我想确认一下费用。",
        }
    )

    processor = HandoffAsrProcessor(transcriber)
    turns = processor.process(
        {
            "agent_id": "agent-1001",
            "customer_recording_path": str(customer_wav),
            "agent_recording_path": str(agent_wav),
        }
    )

    assert turns == [
        {
            "role": "assistant",
            "speaker_type": "human_agent",
            "agent_id": "agent-1001",
            "text": "您好，我是物业客服。",
        },
        {
            "role": "user",
            "speaker_type": "customer",
            "text": "我想确认一下费用。",
        },
    ]


def test_handoff_asr_processor_rejects_missing_recording_path(tmp_path):
    customer_wav = tmp_path / "call-1-customer.wav"
    write_pcm16_wav(customer_wav, b"\x01\x00\x02\x00", sample_rate=8000)

    processor = HandoffAsrProcessor(FakeTranscriber({str(customer_wav): "客户文本"}))

    with pytest.raises(HandoffAsrAdapterError) as exc:
        processor.process(
            {
                "customer_recording_path": str(customer_wav),
                "agent_recording_path": str(tmp_path / "missing-agent.wav"),
            }
        )

    assert str(exc.value) == "agent recording file does not exist"


def test_handoff_asr_http_handler_accepts_gateway_contract(tmp_path):
    customer_wav = tmp_path / "call-1-customer.wav"
    agent_wav = tmp_path / "call-1-agent.wav"
    write_pcm16_wav(customer_wav, b"\x01\x00\x02\x00", sample_rate=8000)
    write_pcm16_wav(agent_wav, b"\x03\x00\x04\x00", sample_rate=8000)
    processor = HandoffAsrProcessor(
        FakeTranscriber(
            {
                str(agent_wav): "您好，我是物业客服。",
                str(customer_wav): "我想确认一下费用。",
            }
        )
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        HandoffAsrHttpHandler.with_processor(processor),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        body = json.dumps(
            {
                "call_id": "call-1",
                "context": {"callId": "business-1"},
                "agent_id": "agent-1001",
                "agent_uuid": "agent-uuid-1",
                "customer_recording_path": str(customer_wav),
                "agent_recording_path": str(agent_wav),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://{server.server_address[0]}:{server.server_address[1]}"
            "/handoff-transcript",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert payload == {
        "turns": [
            {
                "role": "assistant",
                "speaker_type": "human_agent",
                "agent_id": "agent-1001",
                "text": "您好，我是物业客服。",
            },
            {
                "role": "user",
                "speaker_type": "customer",
                "text": "我想确认一下费用。",
            },
        ]
    }


def test_handoff_asr_http_handler_reports_adapter_errors(tmp_path):
    processor = HandoffAsrProcessor(FakeTranscriber({}))
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        HandoffAsrHttpHandler.with_processor(processor),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        request = urllib.request.Request(
            f"http://{server.server_address[0]}:{server.server_address[1]}"
            "/handoff-transcript",
            data=json.dumps(
                {
                    "customer_recording_path": str(tmp_path / "missing-customer.wav"),
                    "agent_recording_path": str(tmp_path / "missing-agent.wav"),
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=2)
        error_payload = json.loads(exc.value.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert exc.value.code == 400
    assert error_payload == {
        "status": "error",
        "error": "customer recording file does not exist",
    }


def test_doubao_audio_transcriber_wraps_probe_timeout(tmp_path):
    wav_path = tmp_path / "agent.wav"
    write_pcm16_wav(wav_path, b"\x01\x00\x02\x00", sample_rate=16000)
    transcriber = DoubaoS2SAudioTranscriber(
        credentials=DoubaoS2SCredentials(
            app_id="app-id",
            access_token="access-token",
        ),
        config=DoubaoS2SSessionConfig(),
        audio_probe_runner=raising_timeout_probe,
    )

    with pytest.raises(HandoffAsrAdapterError) as exc:
        transcriber.transcribe(str(wav_path))

    assert str(exc.value) == "Doubao S2S ASR failed: probe timed out"


class FakeTranscriber:
    def __init__(self, transcripts: dict[str, str]) -> None:
        self.transcripts = transcripts

    def transcribe(self, path: str) -> str:
        return self.transcripts.get(path, "")


async def raising_timeout_probe(*args, **kwargs):
    raise TimeoutError("probe timed out")
