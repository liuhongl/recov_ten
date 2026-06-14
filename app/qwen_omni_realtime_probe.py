from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .billing_comparison_report import build_billing_report
from .env_loader import get_first_env, load_env_file
from .qwen_omni_realtime_client import (
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    DEFAULT_WS_URL,
    MAINLAND_PRICES_RMB_PER_MILLION,
    QwenOmniRealtimeProbeResult,
    QwenOmniRealtimeCredentials,
    QwenOmniRealtimeError,
    QwenOmniRealtimeSessionConfig,
    run_qwen_omni_realtime_audio_probe,
)
from .wav_io import read_wav_as_pcm16_mono, write_pcm16_wav


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Qwen-Omni-Realtime WebSocket audio session"
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--output-dir",
        default="artifacts/qwen-omni-realtime-probe",
    )
    parser.add_argument(
        "--wav",
        required=True,
        help="16-bit PCM WAV input path. Audio is converted to 16 kHz mono.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("QWEN_OMNI_REALTIME_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument(
        "--voice",
        default=os.getenv("QWEN_OMNI_REALTIME_VOICE", DEFAULT_VOICE),
    )
    parser.add_argument(
        "--websocket-url",
        default=os.getenv("QWEN_OMNI_REALTIME_WS_URL", DEFAULT_WS_URL),
    )
    parser.add_argument(
        "--instructions",
        default=os.getenv(
            "QWEN_OMNI_REALTIME_INSTRUCTIONS",
            QwenOmniRealtimeSessionConfig.instructions,
        ),
    )
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--send-delay-ms", type=int, default=20)
    parser.add_argument(
        "--server-vad",
        action="store_true",
        help="Use server VAD instead of deterministic manual commit mode.",
    )
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    api_key = get_first_env(("DASHSCOPE_API_KEY", "QWEN_OMNI_REALTIME_API_KEY"))
    if not api_key:
        print(
            "missing Qwen Omni Realtime credentials in environment: "
            "DASHSCOPE_API_KEY or QWEN_OMNI_REALTIME_API_KEY"
        )
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_pcm, sample_rate = read_wav_as_pcm16_mono(
        args.wav,
        target_sample_rate=QwenOmniRealtimeSessionConfig.input_sample_rate,
    )
    if sample_rate != QwenOmniRealtimeSessionConfig.input_sample_rate:
        raise RuntimeError(f"unexpected sample_rate={sample_rate}")

    credentials = QwenOmniRealtimeCredentials(
        api_key=api_key,
        websocket_url=args.websocket_url,
        model=args.model,
    )
    config = QwenOmniRealtimeSessionConfig(
        voice=args.voice,
        instructions=args.instructions,
        manual_turn_detection=not args.server_vad,
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
    )

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        result, output_audio = asyncio.run(
            run_qwen_omni_realtime_audio_probe(
                credentials,
                config,
                input_pcm16_16k=input_pcm,
                timeout_seconds=args.timeout_seconds,
                chunk_ms=args.chunk_ms,
                send_delay_ms=args.send_delay_ms,
            )
        )
    except QwenOmniRealtimeError as err:
        print(f"Qwen Omni Realtime probe failed: {err}")
        return 1

    output_pcm_path = output_dir / "qwen_omni_realtime_audio_output.pcm"
    output_wav_path = output_dir / "qwen_omni_realtime_audio_output.wav"
    summary_path = output_dir / "qwen_omni_realtime_audio_summary.json"
    billing_report_path = output_dir / "qwen_billing_report.json"

    output_pcm_path.write_bytes(output_audio)
    write_pcm16_wav(
        output_wav_path,
        output_audio,
        sample_rate=result.output_sample_rate,
    )

    summary = asdict(result)
    summary.update(
        {
            "mode": "audio",
            "input_wav_path": str(Path(args.wav)),
            "output_pcm_path": str(output_pcm_path),
            "output_wav_path": str(output_wav_path),
        }
    )
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    billing_report = build_realtime_billing_report_from_result(
        result,
        sample_id=result.session_id or output_dir.name,
        started_at=started_at,
        local_summary_path=str(summary_path),
    )
    billing_report_path.write_text(
        json.dumps(billing_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    console_summary = dict(summary)
    console_summary["sanitized_events_count"] = len(result.sanitized_events)
    console_summary["billing_report_path"] = str(billing_report_path)
    console_summary.pop("sanitized_events", None)
    print(json.dumps(console_summary, ensure_ascii=False, indent=2))
    return 0


def build_realtime_billing_report_from_result(
    result: QwenOmniRealtimeProbeResult,
    *,
    sample_id: str,
    started_at: str,
    local_summary_path: str,
) -> dict[str, object]:
    return build_billing_report(
        sample_id=sample_id,
        started_at=started_at,
        provider="qwen",
        scenario="realtime",
        model=result.model,
        usage={
            "realtime.input_text_tokens": result.usage.input_text_tokens,
            "realtime.input_audio_tokens": result.usage.input_audio_tokens,
            "realtime.output_text_tokens": result.usage.output_text_tokens,
            "realtime.output_audio_tokens": result.usage.output_audio_tokens,
        },
        evidence={
            "local_summary_path": local_summary_path,
            "provider_request_id": result.session_id,
            "console_bill_checked": False,
        },
        price_catalog=_qwen_realtime_price_catalog(result.model),
    )


def _qwen_realtime_price_catalog(model: str) -> dict[str, float]:
    for prefix, prices in MAINLAND_PRICES_RMB_PER_MILLION.items():
        if model == prefix or model.startswith(f"{prefix}-"):
            return {
                "realtime.input_text": prices.input_text_rmb_per_million,
                "realtime.input_audio": prices.input_audio_rmb_per_million,
                "realtime.output_text": prices.output_text_rmb_per_million,
                "realtime.output_audio": prices.output_audio_rmb_per_million,
            }
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
