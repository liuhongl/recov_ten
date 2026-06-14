# Qwen-Omni-Realtime Probe Runbook

This runbook is for isolated Qwen provider experiments. The gateway now has an
experimental `qwen_omni_realtime` provider, but the default remains
`doubao_s2s`.

## Credentials

Set one of these in local `.env` or the shell:

```bash
DASHSCOPE_API_KEY=sk-...
# or
QWEN_OMNI_REALTIME_API_KEY=sk-...
```

Do not commit real keys.

## Gateway Provider Switch

To run the realtime phone gateway against Qwen locally, keep the existing Doubao
opening-audio credentials in place and set:

```bash
REALTIME_PROVIDER=qwen_omni_realtime
DASHSCOPE_API_KEY=sk-...
QWEN_OMNI_REALTIME_MODEL=qwen3-omni-flash-realtime
QWEN_OMNI_REALTIME_VOICE=Cherry
```

The realtime switch changes the realtime dialogue provider only.

Opening audio has its own experimental switch:

```bash
OPENING_AUDIO_PROVIDER=qwen
DASHSCOPE_API_KEY=sk-...
QWEN_OPENING_MODEL=qwen3-tts-flash
QWEN_OPENING_VOICE=Cherry
```

Keep the switches independent when testing. This makes it clear whether a
failure belongs to realtime dialogue or opening TTS.

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
- `qwen_billing_report.json`

The summary includes:

- input/output transcript
- first audio latency and response completion latency
- raw provider `usage`
- line-item RMB estimate for input text, input audio, output text, and output audio
- sanitized provider events without base64 audio payloads

The billing report uses the shared comparison contract and keeps all buckets in
place. Buckets that the realtime probe cannot observe, such as opening and file
ASR, are marked `unavailable`.

## Billing Notes

For Qwen-Omni-Realtime text+audio output, the official pricing table marks the audio-output column as `文本+音频 > 仅音频计费`. The estimator therefore keeps `output_text_tokens` in the summary but marks the `output_text` line item as not billed when audio output is enabled.

Use the provider console bill as the final source of truth after live probes.
