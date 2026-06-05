from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
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

DEFAULT_FILE_ASR_RESOURCE_ID = "volc.seedasr.auc"
DEFAULT_FILE_ASR_SUBMIT_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
)
DEFAULT_FILE_ASR_QUERY_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
)


class HandoffAsrAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptUtterance:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class TranscribedAudio:
    text: str
    utterances: list[TranscriptUtterance]


class AudioTranscriberProtocol(Protocol):
    def transcribe(self, path: str) -> str | TranscribedAudio: ...


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
        turns.extend(
            _turns_from_transcribed_audio(
                _normalize_transcribed_audio(
                    self._transcriber.transcribe(str(agent_path))
                ),
                role="assistant",
                speaker_type="human_agent",
                agent_id=agent_id,
            )
        )
        turns.extend(
            _turns_from_transcribed_audio(
                _normalize_transcribed_audio(
                    self._transcriber.transcribe(str(customer_path))
                ),
                role="user",
                speaker_type="customer",
            )
        )

        if not turns:
            raise HandoffAsrAdapterError("ASR produced no transcript turns")
        turns.sort(
            key=lambda item: (
                item.get("start_ms") is None,
                item.get("start_ms", 0),
            )
        )
        return turns


@dataclass(frozen=True)
class VolcengineFileAsrCredentials:
    api_key: str = ""
    app_key: str = ""
    access_key: str = ""
    resource_id: str = DEFAULT_FILE_ASR_RESOURCE_ID


@dataclass(frozen=True)
class VolcengineFileAsrTranscriber:
    credentials: VolcengineFileAsrCredentials
    uid: str = "sip-realtime-handoff-asr-adapter"
    submit_url: str = DEFAULT_FILE_ASR_SUBMIT_URL
    query_url: str = DEFAULT_FILE_ASR_QUERY_URL
    http_timeout_seconds: float = 15.0
    poll_interval_seconds: float = 2.0
    max_poll_attempts: int = 60
    request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    urlopen: Callable[..., Any] = urllib.request.urlopen
    sleep: Callable[[float], None] = time.sleep

    def transcribe(self, path: str) -> TranscribedAudio:
        return self._transcribe(path)

    def _transcribe(self, path: str) -> TranscribedAudio:
        audio_path = Path(path)
        task_id = self.request_id_factory()
        submit_headers = self._headers(task_id, include_sequence=True)
        audio_payload: dict[str, Any] = {
            "format": _audio_format(audio_path),
            "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
        }
        request_payload: dict[str, Any] = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
        }
        submit_payload = {
            "user": {"uid": self.uid},
            "audio": audio_payload,
            "request": request_payload,
        }
        submit_result = self._post_json(
            self.submit_url,
            submit_payload,
            submit_headers,
            phase="submit",
        )
        if submit_result.status_code != "20000000":
            raise HandoffAsrAdapterError(
                "Volcengine file ASR submit failed: "
                f"status={submit_result.status_code} message={submit_result.message}"
            )

        query_headers = self._headers(task_id, logid=submit_result.logid)
        for _ in range(self.max_poll_attempts):
            query_result = self._post_json(
                self.query_url,
                {},
                query_headers,
                phase="query",
            )
            if query_result.status_code == "20000000":
                return _parse_file_asr_payload(query_result.body)
            if query_result.status_code not in {"20000001", "20000002"}:
                raise HandoffAsrAdapterError(
                    "Volcengine file ASR query failed: "
                    f"status={query_result.status_code} "
                    f"message={query_result.message}"
                )
            self.sleep(self.poll_interval_seconds)
        raise HandoffAsrAdapterError("Volcengine file ASR query timed out")

    def _headers(
        self,
        request_id: str,
        *,
        include_sequence: bool = False,
        logid: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": self.credentials.resource_id,
            "X-Api-Request-Id": request_id,
        }
        if self.credentials.api_key:
            headers["X-Api-Key"] = self.credentials.api_key
        elif self.credentials.app_key and self.credentials.access_key:
            headers["X-Api-App-Key"] = self.credentials.app_key
            headers["X-Api-Access-Key"] = self.credentials.access_key
        else:
            raise HandoffAsrAdapterError("missing Volcengine file ASR credentials")
        if include_sequence:
            headers["X-Api-Sequence"] = "-1"
        if logid:
            headers["X-Tt-Logid"] = logid
        return headers

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        phase: str,
    ) -> "_FileAsrHttpResult":
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with self.urlopen(
                request,
                timeout=self.http_timeout_seconds,
            ) as response:
                body = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            raise HandoffAsrAdapterError(
                f"Volcengine file ASR {phase} HTTP failed: {err.code} {body}"
            ) from err
        except OSError as err:
            raise HandoffAsrAdapterError(
                f"Volcengine file ASR {phase} HTTP failed: {err}"
            ) from err

        status_code = _header_get(response_headers, "X-Api-Status-Code")
        message = _header_get(response_headers, "X-Api-Message") or ""
        logid = _header_get(response_headers, "X-Tt-Logid")
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError as err:
            raise HandoffAsrAdapterError(
                f"Volcengine file ASR {phase} response must be JSON"
            ) from err
        if status_code is None:
            raise HandoffAsrAdapterError(
                f"Volcengine file ASR {phase} response missing status code"
            )
        return _FileAsrHttpResult(
            status_code=status_code,
            message=message,
            logid=logid,
            body=decoded,
        )


@dataclass(frozen=True)
class _FileAsrHttpResult:
    status_code: str
    message: str
    logid: str | None
    body: dict[str, Any]


@dataclass(frozen=True)
class DoubaoS2SAudioTranscriber:
    credentials: DoubaoS2SCredentials
    config: DoubaoS2SSessionConfig
    timeout_seconds: int = 60
    chunk_ms: int = 20
    send_delay_ms: int = 5
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
    parser.add_argument("--http-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--max-poll-attempts", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=60, help=argparse.SUPPRESS)
    parser.add_argument("--chunk-ms", type=int, default=20, help=argparse.SUPPRESS)
    parser.add_argument("--send-delay-ms", type=int, default=5, help=argparse.SUPPRESS)
    parser.add_argument(
        "--trailing-silence-ms",
        type=int,
        default=1200,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    transcriber = VolcengineFileAsrTranscriber(
        credentials=_load_volcengine_file_asr_credentials(),
        uid=os.getenv("DOUBAO_FILE_ASR_UID", "sip-realtime-handoff-asr-adapter"),
        submit_url=os.getenv("DOUBAO_FILE_ASR_SUBMIT_URL", DEFAULT_FILE_ASR_SUBMIT_URL),
        query_url=os.getenv("DOUBAO_FILE_ASR_QUERY_URL", DEFAULT_FILE_ASR_QUERY_URL),
        http_timeout_seconds=args.http_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        max_poll_attempts=args.max_poll_attempts,
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


def _load_volcengine_file_asr_credentials() -> VolcengineFileAsrCredentials:
    api_key = os.getenv("DOUBAO_FILE_ASR_API_KEY") or os.getenv(
        "VOLCENGINE_FILE_ASR_API_KEY",
        "",
    )
    app_key = os.getenv("DOUBAO_FILE_ASR_APP_KEY", "")
    access_key = os.getenv("DOUBAO_FILE_ASR_ACCESS_KEY", "")
    resource_id = os.getenv("DOUBAO_FILE_ASR_RESOURCE_ID", DEFAULT_FILE_ASR_RESOURCE_ID)
    if not api_key and not (app_key and access_key):
        raise RuntimeError(
            "missing Volcengine file ASR credentials in environment: "
            "DOUBAO_FILE_ASR_API_KEY or "
            "DOUBAO_FILE_ASR_APP_KEY + DOUBAO_FILE_ASR_ACCESS_KEY"
        )
    return VolcengineFileAsrCredentials(
        api_key=api_key,
        app_key=app_key,
        access_key=access_key,
        resource_id=resource_id,
    )


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


def _normalize_transcribed_audio(value: str | TranscribedAudio) -> TranscribedAudio:
    if isinstance(value, TranscribedAudio):
        return value
    text = _clean_text(value) or ""
    return TranscribedAudio(text=text, utterances=[])


def _turns_from_transcribed_audio(
    audio: TranscribedAudio,
    *,
    role: str,
    speaker_type: str,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    utterances = audio.utterances or []
    if not utterances:
        text = _clean_text(audio.text)
        if text is None:
            return []
        return [
            _build_turn(
                role=role,
                speaker_type=speaker_type,
                text=text,
                agent_id=agent_id,
            )
        ]

    for utterance in utterances:
        text = _clean_text(utterance.text)
        if text is None:
            continue
        turns.append(
            _build_turn(
                role=role,
                speaker_type=speaker_type,
                text=text,
                agent_id=agent_id,
                start_ms=utterance.start_ms,
                end_ms=utterance.end_ms,
                confidence=utterance.confidence,
            )
        )
    return turns


def _build_turn(
    *,
    role: str,
    speaker_type: str,
    text: str,
    agent_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "role": role,
        "speaker_type": speaker_type,
        "text": text,
    }
    if agent_id is not None:
        turn["agent_id"] = agent_id
    if start_ms is not None:
        turn["start_ms"] = start_ms
    if end_ms is not None:
        turn["end_ms"] = end_ms
    if confidence is not None:
        turn["confidence"] = confidence
    return turn


def _parse_file_asr_payload(payload: dict[str, Any]) -> TranscribedAudio:
    result = payload.get("result")
    if isinstance(result, list):
        result = result[0] if result else {}
    if not isinstance(result, dict):
        raise HandoffAsrAdapterError("Volcengine file ASR response missing result")

    text = _clean_text(result.get("text")) or ""
    utterances: list[TranscriptUtterance] = []
    raw_utterances = result.get("utterances")
    if isinstance(raw_utterances, list):
        for item in raw_utterances:
            if not isinstance(item, dict):
                continue
            utterance_text = _clean_text(item.get("text"))
            if utterance_text is None:
                continue
            utterances.append(
                TranscriptUtterance(
                    text=utterance_text,
                    start_ms=_optional_int(item.get("start_time")),
                    end_ms=_optional_int(item.get("end_time")),
                    confidence=_optional_float(item.get("confidence")),
                )
            )
    if not text and not utterances:
        raise HandoffAsrAdapterError("Volcengine file ASR response has no transcript")
    return TranscribedAudio(text=text, utterances=utterances)


def _audio_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "ogg", "opus"}:
        return suffix
    return "wav"


def _header_get(headers: Any, name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return str(value)
    lower_name = name.lower()
    for key, item in getattr(headers, "items", lambda: [])():
        if str(key).lower() == lower_name:
            return str(item)
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(stripped)
            except ValueError:
                return None
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


if __name__ == "__main__":
    raise SystemExit(main())
