from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from .env_loader import get_first_env, load_env_file
from .qwen_omni_realtime_client import (
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    DEFAULT_WS_URL,
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

    console_summary = dict(summary)
    console_summary["sanitized_events_count"] = len(result.sanitized_events)
    console_summary.pop("sanitized_events", None)
    print(json.dumps(console_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
