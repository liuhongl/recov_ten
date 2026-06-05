from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HumanHandoffTranscriptError(RuntimeError):
    pass


class MockHumanHandoffTranscriptProcessor:
    def process(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        agent_id = _clean_text(job.get("agent_id")) or _clean_text(
            job.get("agent_uuid")
        )
        turn: dict[str, Any] = {
            "role": "assistant",
            "speaker_type": "human_agent",
            "text": "人工坐席已接入并完成通话。",
        }
        if agent_id is not None:
            turn["agent_id"] = agent_id
        return [turn]


class HttpHumanHandoffTranscriptProcessor:
    def __init__(self, url: str, *, timeout_seconds: float) -> None:
        if not url.strip():
            raise ValueError("human transcript HTTP url is required")
        if timeout_seconds <= 0:
            raise ValueError("human transcript timeout must be positive")
        self.url = url
        self.timeout_seconds = timeout_seconds

    def process(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        raw_body = json.dumps(job, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=raw_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                response_body = response.read()
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            raise HumanHandoffTranscriptError(
                f"human transcript HTTP request failed: {err.code} {body}"
            ) from err
        except OSError as err:
            raise HumanHandoffTranscriptError(
                f"human transcript HTTP request failed: {err}"
            ) from err

        try:
            payload = json.loads(response_body.decode("utf-8"))
        except json.JSONDecodeError as err:
            raise HumanHandoffTranscriptError(
                "human transcript HTTP response must be JSON"
            ) from err
        if not isinstance(payload, dict):
            raise HumanHandoffTranscriptError(
                "human transcript HTTP response must be a JSON object"
            )
        turns = _normalize_turns(payload.get("turns"))
        if not turns:
            raise HumanHandoffTranscriptError(
                "human transcript HTTP response must include turns"
            )
        return turns


def _normalize_turns(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    turns: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = _clean_text(item.get("role"))
        text = _clean_text(item.get("text"))
        if role not in {"assistant", "user"} or text is None:
            continue
        turn: dict[str, Any] = {"role": role, "text": text}
        speaker_type = _clean_text(item.get("speaker_type"))
        if speaker_type is not None:
            turn["speaker_type"] = speaker_type
        agent_id = _clean_text(item.get("agent_id"))
        if agent_id is not None:
            turn["agent_id"] = agent_id
        start_ms = _optional_int(item.get("start_ms"))
        if start_ms is not None:
            turn["start_ms"] = start_ms
        end_ms = _optional_int(item.get("end_ms"))
        if end_ms is not None:
            turn["end_ms"] = end_ms
        confidence = _optional_float(item.get("confidence"))
        if confidence is not None:
            turn["confidence"] = confidence
        turns.append(turn)
    return turns


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
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
