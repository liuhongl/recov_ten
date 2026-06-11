from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer

import pytest

from app.handoff_asr_adapter import (
    DoubaoS2SAudioTranscriber,
    HandoffAsrAdapterError,
    HandoffAsrHttpHandler,
    HandoffAsrProcessor,
    TranscribedAudio,
    TranscriptUtterance,
    TranscriptWord,
    VolcengineFileAsrCredentials,
    VolcengineFileAsrTranscriber,
)
from app.doubao_s2s_client import (
    DoubaoS2SCredentials,
    DoubaoS2SProbeResult,
    DoubaoS2SSessionConfig,
)
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


def test_handoff_asr_processor_merges_utterance_turns_by_start_time(tmp_path):
    customer_wav = tmp_path / "call-1-customer.wav"
    agent_wav = tmp_path / "call-1-agent.wav"
    write_pcm16_wav(customer_wav, b"\x01\x00\x02\x00", sample_rate=8000)
    write_pcm16_wav(agent_wav, b"\x03\x00\x04\x00", sample_rate=8000)
    transcriber = FakeTranscriber(
        {
            str(agent_wav): TranscribedAudio(
                text="您好，我是物业客服。请问哪里需要核实？",
                utterances=[
                    TranscriptUtterance(
                        text="您好，我是物业客服。",
                        start_ms=1500,
                        end_ms=2600,
                    ),
                    TranscriptUtterance(
                        text="请问哪里需要核实？",
                        start_ms=4300,
                        end_ms=5600,
                        confidence=0.92,
                    ),
                ],
            ),
            str(customer_wav): TranscribedAudio(
                text="我想确认一下费用。",
                utterances=[
                    TranscriptUtterance(
                        text="我想确认一下费用。",
                        start_ms=900,
                        end_ms=1400,
                    )
                ],
            ),
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
            "role": "user",
            "speaker_type": "customer",
            "text": "我想确认一下费用。",
            "start_ms": 900,
            "end_ms": 1400,
        },
        {
            "role": "assistant",
            "speaker_type": "human_agent",
            "agent_id": "agent-1001",
            "text": "您好，我是物业客服。",
            "start_ms": 1500,
            "end_ms": 2600,
        },
        {
            "role": "assistant",
            "speaker_type": "human_agent",
            "agent_id": "agent-1001",
            "text": "请问哪里需要核实？",
            "start_ms": 4300,
            "end_ms": 5600,
            "confidence": 0.92,
        },
    ]


def test_handoff_asr_processor_splits_word_timed_handoff_dialog_turns(tmp_path):
    customer_wav = tmp_path / "call-1-customer.wav"
    agent_wav = tmp_path / "call-1-agent.wav"
    write_pcm16_wav(customer_wav, b"\x01\x00\x02\x00", sample_rate=8000)
    write_pcm16_wav(agent_wav, b"\x03\x00\x04\x00", sample_rate=8000)
    transcriber = FakeTranscriber(
        {
            str(agent_wav): TranscribedAudio(
                text="一二三四五。四五六七八明天下雨吗？明天天气怎么样？",
                utterances=[
                    TranscriptUtterance(
                        text="一二三四五。",
                        start_ms=9590,
                        end_ms=11070,
                        words=_words(
                            [
                                ("一", 9590, 9700),
                                ("二", 9800, 9900),
                                ("三", 10000, 10100),
                                ("四", 10200, 10300),
                                ("五", 10400, 10500),
                            ]
                        ),
                    ),
                    TranscriptUtterance(
                        text="四五六七八明天下雨吗？明天天气怎么样？",
                        start_ms=16120,
                        end_ms=27480,
                        words=_words(
                            [
                                ("四", 16120, 16300),
                                ("五", 16400, 16600),
                                ("六", 16700, 16900),
                                ("七", 17000, 17200),
                                ("八", 17300, 17500),
                                ("明", 21000, 21200),
                                ("天", 21200, 21400),
                                ("下", 21400, 21600),
                                ("雨", 21600, 21800),
                                ("吗", 21800, 22000),
                                ("明", 26000, 26200),
                                ("天", 26200, 26400),
                                ("天", 26400, 26600),
                                ("气", 26600, 26800),
                                ("怎", 26800, 27000),
                                ("么", 27000, 27200),
                                ("样", 27200, 27400),
                            ]
                        ),
                    ),
                ],
            ),
            str(customer_wav): TranscribedAudio(
                text="你好，你是谁？明天不下雨，明天天气挺好的。晴空万里万里无云。",
                utterances=[
                    TranscriptUtterance(
                        text="你好，你是谁？",
                        start_ms=18890,
                        end_ms=20450,
                        words=_words(
                            [
                                ("你", 18890, 19000),
                                ("好", 19000, 19100),
                                ("你", 19400, 19500),
                                ("是", 19500, 19600),
                                ("谁", 19600, 19800),
                            ]
                        ),
                    ),
                    TranscriptUtterance(
                        text="明天不下雨，明天天气挺好的。晴空万里万里无云。",
                        start_ms=22700,
                        end_ms=30200,
                        words=_words(
                            [
                                ("明", 22700, 22900),
                                ("天", 22900, 23100),
                                ("不", 23100, 23300),
                                ("下", 23300, 23500),
                                ("雨", 23500, 23700),
                                ("明", 23800, 24000),
                                ("天", 24000, 24200),
                                ("天", 24200, 24400),
                                ("气", 24400, 24600),
                                ("挺", 24600, 24800),
                                ("好", 24800, 25000),
                                ("的", 25000, 25200),
                                ("晴", 28600, 28800),
                                ("空", 28800, 29000),
                                ("万", 29000, 29200),
                                ("里", 29200, 29400),
                                ("万", 29400, 29600),
                                ("里", 29600, 29800),
                                ("无", 29800, 30000),
                                ("云", 30000, 30200),
                            ]
                        ),
                    ),
                ],
            ),
        }
    )

    processor = HandoffAsrProcessor(transcriber)
    turns = processor.process(
        {
            "agent_id": "1001",
            "customer_recording_path": str(customer_wav),
            "agent_recording_path": str(agent_wav),
        }
    )

    assert [(turn["speaker_type"], turn["text"]) for turn in turns] == [
        ("human_agent", "一二三四五。"),
        ("human_agent", "四五六七八"),
        ("customer", "你好，你是谁？"),
        ("human_agent", "明天下雨吗？"),
        ("customer", "明天不下雨，明天天气挺好的。"),
        ("human_agent", "明天天气怎么样？"),
        ("customer", "晴空万里万里无云。"),
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


def test_volcengine_file_asr_transcriber_submits_wav_and_parses_utterances(tmp_path):
    wav_path = tmp_path / "agent.wav"
    write_pcm16_wav(wav_path, b"\x01\x00\x02\x00", sample_rate=8000)
    calls = []

    def fake_urlopen(request, *, timeout):
        body = json.loads(request.data.decode("utf-8"))
        calls.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": body,
                "timeout": timeout,
            }
        )
        if request.full_url.endswith("/submit"):
            return FakeHttpResponse(
                b"",
                {
                    "X-Api-Status-Code": "20000000",
                    "X-Api-Message": "OK",
                    "X-Tt-Logid": "log-1",
                },
            )
        return FakeHttpResponse(
            json.dumps(
                {
                    "result": {
                        "text": "您好，我是物业客服。",
                        "utterances": [
                            {
                                "text": "您好，我是物业客服。",
                                "start_time": 1200,
                                "end_time": 2600,
                                "words": [
                                    {
                                        "text": "您",
                                        "start_time": 1200,
                                        "end_time": 1320,
                                    },
                                    {
                                        "text": "好",
                                        "start_time": 1320,
                                        "end_time": 1440,
                                    },
                                ],
                            }
                        ],
                    }
                }
            ).encode("utf-8"),
            {
                "X-Api-Status-Code": "20000000",
                "X-Api-Message": "OK",
                "X-Tt-Logid": "log-2",
            },
        )

    transcriber = VolcengineFileAsrTranscriber(
        credentials=VolcengineFileAsrCredentials(api_key="api-key"),
        request_id_factory=lambda: "task-1",
        urlopen=fake_urlopen,
        sleep=lambda _: None,
    )

    result = transcriber.transcribe(str(wav_path))

    assert result == TranscribedAudio(
        text="您好，我是物业客服。",
        utterances=[
            TranscriptUtterance(
                text="您好，我是物业客服。",
                start_ms=1200,
                end_ms=2600,
                words=[
                    TranscriptWord(text="您", start_ms=1200, end_ms=1320),
                    TranscriptWord(text="好", start_ms=1320, end_ms=1440),
                ],
            )
        ],
    )
    assert [call["url"] for call in calls] == [
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query",
    ]
    assert calls[0]["headers"]["X-api-key"] == "api-key"
    assert calls[0]["headers"]["X-api-resource-id"] == "volc.seedasr.auc"
    assert calls[0]["headers"]["X-api-request-id"] == "task-1"
    assert calls[0]["headers"]["X-api-sequence"] == "-1"
    assert base64.b64decode(calls[0]["body"]["audio"]["data"]).startswith(b"RIFF")
    assert calls[0]["body"]["audio"]["format"] == "wav"
    assert calls[0]["body"]["request"]["show_utterances"] is True
    assert calls[1]["headers"]["X-api-request-id"] == "task-1"
    assert calls[1]["body"] == {}


def test_volcengine_file_asr_transcriber_polls_until_completed(tmp_path):
    wav_path = tmp_path / "agent.wav"
    write_pcm16_wav(wav_path, b"\x01\x00\x02\x00", sample_rate=8000)
    query_statuses = ["20000002", "20000000"]
    sleeps = []

    def fake_urlopen(request, *, timeout):
        if request.full_url.endswith("/submit"):
            return FakeHttpResponse(
                b"",
                {
                    "X-Api-Status-Code": "20000000",
                    "X-Api-Message": "OK",
                    "X-Tt-Logid": "submit-log",
                },
            )
        status = query_statuses.pop(0)
        return FakeHttpResponse(
            json.dumps({"result": {"text": "客户文本。"}}).encode("utf-8"),
            {
                "X-Api-Status-Code": status,
                "X-Api-Message": "OK",
                "X-Tt-Logid": "query-log",
            },
        )

    transcriber = VolcengineFileAsrTranscriber(
        credentials=VolcengineFileAsrCredentials(api_key="api-key"),
        request_id_factory=lambda: "task-1",
        urlopen=fake_urlopen,
        sleep=sleeps.append,
        poll_interval_seconds=0.25,
    )

    assert transcriber.transcribe(str(wav_path)) == TranscribedAudio(
        text="客户文本。",
        utterances=[],
    )
    assert sleeps == [0.25]


def test_volcengine_file_asr_transcriber_reports_provider_failure(tmp_path):
    wav_path = tmp_path / "agent.wav"
    write_pcm16_wav(wav_path, b"\x01\x00\x02\x00", sample_rate=8000)

    def fake_urlopen(request, *, timeout):
        return FakeHttpResponse(
            b"",
            {
                "X-Api-Status-Code": "45000002",
                "X-Api-Message": "empty audio",
                "X-Tt-Logid": "log-1",
            },
        )

    transcriber = VolcengineFileAsrTranscriber(
        credentials=VolcengineFileAsrCredentials(api_key="api-key"),
        request_id_factory=lambda: "task-1",
        urlopen=fake_urlopen,
        sleep=lambda _: None,
    )

    with pytest.raises(HandoffAsrAdapterError) as exc:
        transcriber.transcribe(str(wav_path))

    assert str(exc.value) == (
        "Volcengine file ASR submit failed: status=45000002 message=empty audio"
    )


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


def test_doubao_audio_transcriber_defaults_to_small_pacing_delay(tmp_path):
    wav_path = tmp_path / "agent.wav"
    write_pcm16_wav(wav_path, b"\x01\x00\x02\x00", sample_rate=16000)
    seen_kwargs = {}
    transcriber = DoubaoS2SAudioTranscriber(
        credentials=DoubaoS2SCredentials(
            app_id="app-id",
            access_token="access-token",
        ),
        config=DoubaoS2SSessionConfig(),
        audio_probe_runner=capturing_probe(seen_kwargs),
    )

    assert transcriber.transcribe(str(wav_path)) == "您好。"

    assert seen_kwargs["send_delay_ms"] == 5


class FakeTranscriber:
    def __init__(self, transcripts: dict[str, str | TranscribedAudio]) -> None:
        self.transcripts = transcripts

    def transcribe(self, path: str) -> TranscribedAudio:
        value = self.transcripts.get(path, "")
        if isinstance(value, TranscribedAudio):
            return value
        return TranscribedAudio(text=value, utterances=[])


def _words(items):
    return [
        TranscriptWord(text=text, start_ms=start_ms, end_ms=end_ms)
        for text, start_ms, end_ms in items
    ]


class FakeHttpResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.body = body
        self.headers = headers
        self.status = HTTPStatus.OK

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.body


async def raising_timeout_probe(*args, **kwargs):
    raise TimeoutError("probe timed out")


def capturing_probe(seen_kwargs):
    async def probe(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return (
            DoubaoS2SProbeResult(
                session_id="session-1",
                speaker="speaker-1",
                input_text="",
                input_audio_bytes=len(kwargs["input_pcm16_16k"]),
                output_audio_bytes=0,
                input_transcript="您好。",
                output_transcript="",
                event_counts={},
                sanitized_events=[],
                first_audio_delta_ms=None,
                response_done_ms=None,
            ),
            b"",
        )

    return probe
