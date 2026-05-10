from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

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
    run_doubao_s2s_text_probe,
)
from .env_loader import load_env_file
from .wav_io import read_wav_as_pcm16_mono, write_pcm16_wav


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Doubao S2S realtime voice WebSocket"
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--output-dir", default="artifacts/doubao-s2s-probe")
    parser.add_argument(
        "--text",
        default="请用一句话介绍你自己。",
        help="Text query for a text-to-voice probe.",
    )
    parser.add_argument(
        "--wav",
        default="",
        help="Optional 16-bit PCM WAV path for an audio-to-voice probe.",
    )
    parser.add_argument("--speaker", default=os.getenv("DOUBAO_S2S_SPEAKER", ""))
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--send-delay-ms", type=int, default=20)
    parser.add_argument("--trailing-silence-ms", type=int, default=1200)
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    credentials = _load_credentials()
    config = DoubaoS2SSessionConfig(
        speaker=args.speaker or os.getenv("DOUBAO_S2S_SPEAKER", DEFAULT_SPEAKER),
        system_prompt=os.getenv(
            "DOUBAO_S2S_SYSTEM_PROMPT",
            DoubaoS2SSessionConfig.system_prompt,
        ),
        uid=os.getenv("DOUBAO_S2S_UID", "sip-realtime-voice-gateway"),
    )

    if args.wav:
        input_pcm, sample_rate = read_wav_as_pcm16_mono(
            args.wav,
            target_sample_rate=DEFAULT_SAMPLE_RATE,
        )
        if sample_rate != DEFAULT_SAMPLE_RATE:
            raise RuntimeError(f"unexpected sample_rate={sample_rate}")
        try:
            result, output_audio = asyncio.run(
                run_doubao_s2s_audio_probe(
                    credentials,
                    config,
                    input_pcm16_16k=input_pcm,
                    timeout_seconds=args.timeout_seconds,
                    chunk_ms=args.chunk_ms,
                    send_delay_ms=args.send_delay_ms,
                    trailing_silence_ms=args.trailing_silence_ms,
                )
            )
        except DoubaoS2SError as err:
            print(f"Doubao S2S probe failed: {err}")
            return 1
        mode = "audio"
    else:
        try:
            result, output_audio = asyncio.run(
                run_doubao_s2s_text_probe(
                    credentials,
                    config,
                    input_text=args.text,
                    timeout_seconds=args.timeout_seconds,
                )
            )
        except DoubaoS2SError as err:
            print(f"Doubao S2S probe failed: {err}")
            return 1
        mode = "text"

    output_pcm_path = output_dir / f"doubao_s2s_{mode}_output.pcm"
    output_wav_path = output_dir / f"doubao_s2s_{mode}_output.wav"
    summary_path = output_dir / f"doubao_s2s_{mode}_summary.json"

    output_pcm_path.write_bytes(output_audio)
    write_pcm16_wav(
        output_wav_path,
        output_audio,
        sample_rate=result.output_sample_rate,
    )

    summary = asdict(result)
    summary.update(
        {
            "mode": mode,
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


def _load_credentials() -> DoubaoS2SCredentials:
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


if __name__ == "__main__":
    raise SystemExit(main())
