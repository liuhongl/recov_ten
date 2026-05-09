from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .config import load_config
from .env_loader import get_first_env, load_env_file
from .realtime_client import DEFAULT_OUTPUT_SAMPLE_RATE, run_realtime_probe
from .realtime_client import DEFAULT_INPUT_SAMPLE_RATE
from .wav_io import read_wav_as_pcm16_mono, write_pcm16_wav

DEFAULT_ENV_NAMES = ("DASHSCOPE_API_KEY", "ALIYUN_DASHSCOPE_API_KEY")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an offline realtime model probe")
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
    parser.add_argument("--output-dir", default="artifacts/stage4")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--send-delay-ms", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--instructions",
        default="你是中文电话智能客服。请用一句简短、自然的中文回答用户。",
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
        _run_and_collect_output(config.realtime, api_key, input_pcm, args)
    )

    output_pcm_path = output_dir / "realtime_output_24k.pcm"
    output_wav_path = output_dir / "realtime_output_24k.wav"
    summary_path = output_dir / "realtime_probe_summary.json"

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

    print(
        json.dumps(
            {
                "model": result.model,
                "voice": result.voice,
                "input_audio_bytes": result.input_audio_bytes,
                "output_audio_bytes": result.output_audio_bytes,
                "input_transcript": result.input_transcript,
                "output_transcript": result.output_transcript,
                "first_audio_delta_ms": result.first_audio_delta_ms,
                "response_done_ms": result.response_done_ms,
                "output_pcm": str(output_pcm_path),
                "output_wav": str(output_wav_path),
                "summary": str(summary_path),
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
        normalized_pcm_path = output_dir / "realtime_input_16k.pcm"
        normalized_wav_path = output_dir / "realtime_input_16k.wav"
        normalized_pcm_path.write_bytes(input_pcm)
        write_pcm16_wav(
            normalized_wav_path,
            input_pcm,
            sample_rate=sample_rate,
        )
        return input_pcm

    return Path(args.input_pcm).read_bytes()


async def _run_and_collect_output(realtime_config, api_key, input_pcm, args):
    return await run_realtime_probe(
        realtime_config,
        api_key=api_key,
        input_pcm=input_pcm,
        instructions=args.instructions,
        chunk_ms=args.chunk_ms,
        timeout_seconds=args.timeout,
        send_delay_ms=args.send_delay_ms,
    )


if __name__ == "__main__":
    raise SystemExit(main())
