from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from .doubao_s2s_client import (
    DEFAULT_REALTIME_APP_KEY,
    DEFAULT_RESOURCE_ID,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SPEAKER,
    DEFAULT_WS_URL,
    DoubaoS2SCredentials,
    DoubaoS2SError,
    DoubaoS2SSessionConfig,
    run_doubao_s2s_audio_probe,
)
from .env_loader import load_env_file
from .wav_io import read_wav_as_pcm16_mono


class HandoffAsrAdapterError(RuntimeError):
    pass


class AudioTranscriberProtocol(Protocol):
    def transcribe(self, path: str) -> str: ...


class HandoffAsrProcessor:
    def __init__(self, transcriber: AudioTranscriberProtocol) -> None:
        self._transcriber = transcriber

    def process(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        customer_path = _required_existing_path(
            job.get("customer_recording_path"),
            "customer recording",
        )
        agent_path = _required_existing_path(
            job.get("agent_recording_path"),
            "agent recording",
        )
        agent_id = _clean_text(job.get("agent_id")) or _clean_text(
            job.get("agent_uuid")
        )

        turns: list[dict[str, Any]] = []
        agent_text = _clean_text(self._transcriber.transcribe(str(agent_path)))
        if agent_text is not None:
            agent_turn: dict[str, Any] = {
                "role": "assistant",
                "speaker_type": "human_agent",
                "text": agent_text,
            }
            if agent_id is not None:
                agent_turn["agent_id"] = agent_id
            turns.append(agent_turn)

        customer_text = _clean_text(self._transcriber.transcribe(str(customer_path)))
        if customer_text is not None:
            turns.append(
                {
                    "role": "user",
                    "speaker_type": "customer",
                    "text": customer_text,
                }
            )

        if not turns:
            raise HandoffAsrAdapterError("ASR produced no transcript turns")
        return turns


@dataclass(frozen=True)
class DoubaoS2SAudioTranscriber:
    credentials: DoubaoS2SCredentials
    config: DoubaoS2SSessionConfig
    timeout_seconds: int = 60
    chunk_ms: int = 20
    send_delay_ms: int = 0
    trailing_silence_ms: int = 1200
    audio_probe_runner: Callable[..., Any] = run_doubao_s2s_audio_probe

    def transcribe(self, path: str) -> str:
        pcm, sample_rate = read_wav_as_pcm16_mono(
            path,
            target_sample_rate=DEFAULT_SAMPLE_RATE,
        )
        if sample_rate != DEFAULT_SAMPLE_RATE:
            raise HandoffAsrAdapterError(f"unexpected sample_rate={sample_rate}")
        try:
            result, _ = asyncio.run(
                self.audio_probe_runner(
                    self.credentials,
                    self.config,
                    input_pcm16_16k=pcm,
                    timeout_seconds=self.timeout_seconds,
                    chunk_ms=self.chunk_ms,
                    send_delay_ms=self.send_delay_ms,
                    trailing_silence_ms=self.trailing_silence_ms,
                )
            )
        except (DoubaoS2SError, OSError, TimeoutError, ValueError) as err:
            raise HandoffAsrAdapterError(f"Doubao S2S ASR failed: {err}") from err
        return result.input_transcript


class HandoffAsrHttpHandler(BaseHTTPRequestHandler):
    processor: HandoffAsrProcessor | None = None

    @classmethod
    def with_processor(cls, processor: HandoffAsrProcessor):
        class BoundHandoffAsrHttpHandler(cls):
            pass

        BoundHandoffAsrHttpHandler.processor = processor
        return BoundHandoffAsrHttpHandler

    def do_POST(self) -> None:
        if self.path != "/handoff-transcript":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"status": "error", "error": "not found"},
            )
            return
        if self.processor is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"status": "error", "error": "ASR processor unavailable"},
            )
            return

        try:
            job = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "error": "invalid JSON body"},
            )
            return
        if not isinstance(job, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "error": "JSON body must be an object"},
            )
            return

        try:
            turns = self.processor.process(job)
        except HandoffAsrAdapterError as err:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "error": str(err)},
            )
            return

        self._send_json(HTTPStatus.OK, {"turns": turns})

    def log_message(self, format, *args) -> None:
        return

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HTTP ASR adapter for human handoff recordings"
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9200)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--send-delay-ms", type=int, default=0)
    parser.add_argument("--trailing-silence-ms", type=int, default=1200)
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    transcriber = DoubaoS2SAudioTranscriber(
        credentials=_load_doubao_credentials(),
        config=_load_doubao_session_config(),
        timeout_seconds=args.timeout_seconds,
        chunk_ms=args.chunk_ms,
        send_delay_ms=args.send_delay_ms,
        trailing_silence_ms=args.trailing_silence_ms,
    )
    processor = HandoffAsrProcessor(transcriber)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        HandoffAsrHttpHandler.with_processor(processor),
    )
    print(f"handoff ASR adapter listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _load_doubao_credentials() -> DoubaoS2SCredentials:
    app_id = os.getenv("DOUBAO_S2S_APP_ID", "")
    access_token = os.getenv("DOUBAO_S2S_ACCESS_TOKEN", "")
    app_key = (
        os.getenv("DOUBAO_S2S_APP_KEY")
        or os.getenv("DOUBAO_S2S_SECRET_KEY")
        or DEFAULT_REALTIME_APP_KEY
    )
    resource_id = os.getenv("DOUBAO_S2S_RESOURCE_ID", DEFAULT_RESOURCE_ID)
    websocket_url = os.getenv("DOUBAO_S2S_WS_URL", DEFAULT_WS_URL)
    missing = []
    if not app_id:
        missing.append("DOUBAO_S2S_APP_ID")
    if not access_token:
        missing.append("DOUBAO_S2S_ACCESS_TOKEN")
    if missing:
        raise RuntimeError(
            "missing Doubao S2S credentials in environment: " + ", ".join(missing)
        )
    return DoubaoS2SCredentials(
        app_id=app_id,
        access_token=access_token,
        app_key=app_key,
        resource_id=resource_id,
        websocket_url=websocket_url,
    )


def _load_doubao_session_config() -> DoubaoS2SSessionConfig:
    return DoubaoS2SSessionConfig(
        speaker=os.getenv("DOUBAO_S2S_SPEAKER", DEFAULT_SPEAKER),
        system_prompt=(
            "你是电话录音转写助手。请只识别用户输入音频中的中文语音内容，"
            "不要扩写，不要总结。"
        ),
        uid=os.getenv("DOUBAO_S2S_UID", "sip-realtime-handoff-asr-adapter"),
    )


def _required_existing_path(value: object, label: str) -> Path:
    text = _clean_text(value)
    if text is None:
        raise HandoffAsrAdapterError(f"{label} path is required")
    path = Path(text)
    if not path.is_file():
        raise HandoffAsrAdapterError(f"{label} file does not exist")
    return path


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


if __name__ == "__main__":
    raise SystemExit(main())
