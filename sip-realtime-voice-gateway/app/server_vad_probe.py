from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .config import load_config
from .env_loader import get_first_env, load_env_file
from .realtime_client import (
    DEFAULT_INPUT_SAMPLE_RATE,
    DEFAULT_OUTPUT_SAMPLE_RATE,
    run_server_vad_probe,
)
from .wav_io import read_wav_as_pcm16_mono, write_pcm16_wav

DEFAULT_ENV_NAMES = ("DASHSCOPE_API_KEY", "ALIYUN_DASHSCOPE_API_KEY")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an offline Qwen Realtime Server VAD probe"
    )
    parser.add_argument("--config", default="configs/local.example.toml")
    parser.add_argument("--env-file", default="../ai_agents/.env")
    parser.add_argument(
        "--input-pcm",
        default="../ai_agents/agents/integration_tests/asr_guarder/tests/test_data/16k_zh_cn.pcm",
        help="16kHz mono pcm_s16le input audio",
    )
    parser.add_argument(
        "--input-wav",
        default=None,
        help="Input WAV file; converted to 16kHz mono pcm_s16le before sending",
    )
    parser.add_argument("--output-dir", default="artifacts/stage-a1-server-vad")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--send-delay-ms", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--silence-duration-ms", type=int, default=800)
    parser.add_argument("--prefix-padding-ms", type=int, default=300)
    parser.add_argument("--trailing-silence-ms", type=int, default=1200)
    parser.add_argument(
        "--instructions",
        default=(
            "你是中文电话智能客服。请用一句简短、自然的中文回答用户。"
        ),
    )
    args = parser.parse_args()

    load_env_file(args.env_file)
    api_key = get_first_env(DEFAULT_ENV_NAMES)
    if api_key is None:
        raise RuntimeError(
            "missing DashScope API key; set DASHSCOPE_API_KEY or "
            "ALIYUN_DASHSCOPE_API_KEY"
        )

    config = load_config(args.config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_pcm = _load_input_audio(args, output_dir)

    result, output_pcm = asyncio.run(
        run_server_vad_probe(
            config.realtime,
            api_key=api_key,
            input_pcm=input_pcm,
            instructions=args.instructions,
            threshold=args.threshold,
            silence_duration_ms=args.silence_duration_ms,
            prefix_padding_ms=args.prefix_padding_ms,
            trailing_silence_ms=args.trailing_silence_ms,
            chunk_ms=args.chunk_ms,
            timeout_seconds=args.timeout,
            send_delay_ms=args.send_delay_ms,
        )
    )

    output_pcm_path = output_dir / "server_vad_output_24k.pcm"
    output_wav_path = output_dir / "server_vad_output_24k.wav"
    summary_path = output_dir / "server_vad_probe_summary.json"
    events_path = output_dir / "server_vad_events.jsonl"

    output_pcm_path.write_bytes(output_pcm)
    write_pcm16_wav(
        output_wav_path,
        output_pcm,
        sample_rate=DEFAULT_OUTPUT_SAMPLE_RATE,
    )
    summary_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    events_path.write_text(
        "\n".join(
            json.dumps(event, ensure_ascii=False) for event in result.sanitized_events
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "model": result.model,
                "voice": result.voice,
                "input_audio_bytes": result.input_audio_bytes,
                "trailing_silence_ms": result.trailing_silence_ms,
                "output_audio_bytes": result.output_audio_bytes,
                "input_transcript": result.input_transcript,
                "output_transcript": result.output_transcript,
                "response_ids": result.response_ids,
                "speech_started_event_ms": result.speech_started_event_ms,
                "speech_stopped_event_ms": result.speech_stopped_event_ms,
                "response_created_ms": result.response_created_ms,
                "first_audio_delta_ms": result.first_audio_delta_ms,
                "response_done_ms": result.response_done_ms,
                "output_pcm": str(output_pcm_path),
                "output_wav": str(output_wav_path),
                "summary": str(summary_path),
                "events": str(events_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _load_input_audio(args, output_dir: Path) -> bytes:
    if args.input_wav:
        input_pcm, sample_rate = read_wav_as_pcm16_mono(
            args.input_wav,
            target_sample_rate=DEFAULT_INPUT_SAMPLE_RATE,
        )
        normalized_pcm_path = output_dir / "server_vad_input_16k.pcm"
        normalized_wav_path = output_dir / "server_vad_input_16k.wav"
        normalized_pcm_path.write_bytes(input_pcm)
        write_pcm16_wav(
            normalized_wav_path,
            input_pcm,
            sample_rate=sample_rate,
        )
        return input_pcm

    return Path(args.input_pcm).read_bytes()


if __name__ == "__main__":
    raise SystemExit(main())
