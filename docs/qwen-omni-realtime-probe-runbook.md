# Qwen-Omni-Realtime Probe Runbook

This runbook is for isolated Qwen provider experiments. It does not switch the production gateway away from Doubao S2S.

## Credentials

Set one of these in local `.env` or the shell:

```bash
DASHSCOPE_API_KEY=sk-...
# or
QWEN_OMNI_REALTIME_API_KEY=sk-...
```

Do not commit real keys.

## Audio Probe

Use a short 16-bit PCM WAV. The probe converts it to 16 kHz mono before sending.

```bash
uv run python -m app.qwen_omni_realtime_probe \
  --wav /path/to/customer-sample.wav \
  --model qwen3-omni-flash-realtime \
  --voice Cherry \
  --output-dir artifacts/qwen-omni-realtime-probe
```

Outputs:

- `qwen_omni_realtime_audio_output.pcm`
- `qwen_omni_realtime_audio_output.wav`
- `qwen_omni_realtime_audio_summary.json`

The summary includes:

- input/output transcript
- first audio latency and response completion latency
- raw provider `usage`
- line-item RMB estimate for input text, input audio, output text, and output audio
- sanitized provider events without base64 audio payloads

## Billing Notes

For Qwen-Omni-Realtime text+audio output, the official pricing table marks the audio-output column as `文本+音频 > 仅音频计费`. The estimator therefore keeps `output_text_tokens` in the summary but marks the `output_text` line item as not billed when audio output is enabled.

Use the provider console bill as the final source of truth after live probes.
