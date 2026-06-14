# Qwen Billing Comparison Design

## Goal

Move the paid Doubao speech capabilities used by the current gateway onto
Qwen/Aliyun models as an isolated experiment, then produce a billing comparison
that separates the real cost buckets instead of hiding them behind one total.

The experiment must not change production defaults. Production remains on the
existing Doubao/Volcengine paths unless an explicit config or environment
switch enables Qwen.

## Current State

The `codex/compare-qwen-omni-realtime` branch already contains:

- `qwen_omni_realtime` as an experimental realtime dialogue provider.
- A Qwen Realtime audio probe that records provider `usage` and estimates
  input text, input audio, output text, and output audio cost.
- A provider switch for realtime dialogue only.

The branch does not yet replace:

- Opening audio generation. It still uses `DoubaoTTSOpeningAudioGenerator` with
  `DoubaoOpeningAudioGenerator` as the fallback.
- Human handoff/file ASR. The HTTP adapter still uses
  `VolcengineFileAsrTranscriber`.
- A common billing report across realtime, opening, and file ASR.

## Paid Capabilities To Compare

### Realtime Dialogue

Existing Doubao capability:

- Server-side realtime speech dialogue over WebSocket.
- The provider receives customer audio and returns assistant audio.
- Cost must be tracked as input audio, input text/context, output text, and
  output audio where the provider exposes usage.

Qwen target:

- `qwen_omni_realtime`, initially `qwen3-omni-flash-realtime`.
- Existing probe and gateway provider switch remain the foundation.

### Opening Audio

Existing Doubao capability:

- Preferred path: Doubao TTS HTTP API.
- Fallback path: Doubao S2S text probe used as a TTS-like generator.

Qwen target:

- A new `QwenOpeningAudioGenerator` behind the existing
  `OpeningAudioGenerator` protocol.
- First implementation should use the smallest stable Aliyun/Qwen API that can
  turn a short opening text into PCM/WAV audio with a selectable voice.
- The generator must return the same `OpeningAudio` contract:
  `pcm16`, `sample_rate`, and `generation_ms`.

The opening provider is a separate switch from realtime. This keeps failures
attributable: a Qwen realtime test can run while opening remains Doubao, and a
Qwen opening test can run without changing live dialogue.

### File ASR / Handoff ASR

Existing Doubao/Volcengine capability:

- `app.handoff_asr_adapter` exposes `/handoff-transcript`.
- The gateway posts two mono WAV paths: customer and agent.
- The adapter reads local files, submits each side to Volcengine file ASR, and
  returns the existing `turns` contract.

Qwen target:

- Add a `QwenFileAsrTranscriber` that implements the existing
  `AudioTranscriberProtocol`.
- Keep `/handoff-transcript` request and response unchanged.
- Preserve dual mono behavior: transcribe customer and agent WAV separately,
  then merge turns by timestamps when available.
- Use `qwen3-asr-flash-filetrans` or the configured Aliyun ASR model for
  asynchronous file transcription.

If the selected Aliyun API requires a URL instead of raw bytes, the first
implementation may support local file upload via SDK for tests and small
traffic, while documenting that production should use OSS-backed URLs for
scale.

## Billing Buckets

Billing comparison output must include these buckets:

- `realtime.input_text`
- `realtime.input_audio`
- `realtime.output_text`
- `realtime.output_audio`
- `opening.input_text`
- `opening.output_audio`
- `file_asr.audio_seconds`

Provider reports may not expose all buckets. Missing fields must be reported as
`unavailable`, not guessed silently.

Qwen Realtime rules:

- Use provider `usage` from `response.done` when available.
- For `qwen3-omni-flash-realtime`, official docs say audio token conversion is
  based on audio seconds; insufficient sub-second audio is rounded by the
  provider. The local estimator is only a preview until checked against the
  provider bill.
- When text+audio output is enabled, the Qwen pricing table marks output text
  as not separately billed for the audio-output mode. The report should still
  display output text tokens for observability but mark that line as not billed.

Qwen file ASR rules:

- Use the model's seconds-based ASR price.
- Report per-side duration and total duration for dual mono.

Doubao rules:

- Use available provider usage and/or the provider's published billing unit for
  the same capability.
- If exact token/second usage is unavailable from the local API response, record
  the provider console bill as the source of truth.

## Report Contract

Add a JSON report format that can be produced by probes and live samples:

```json
{
  "sample_id": "qwen-billing-001",
  "started_at": "2026-06-14T00:00:00Z",
  "provider": "qwen",
  "scenario": "opening|realtime|file_asr|full_call",
  "model": "qwen3-omni-flash-realtime",
  "usage": {
    "realtime.input_audio_tokens": 0,
    "realtime.output_audio_tokens": 0,
    "opening.output_audio_seconds": 0,
    "file_asr.audio_seconds": 0
  },
  "estimate": {
    "currency": "CNY",
    "total": 0.0,
    "line_items": []
  },
  "evidence": {
    "local_summary_path": "",
    "provider_request_id": "",
    "call_record_id": "",
    "console_bill_checked": false
  }
}
```

The report is intentionally local-file first. Database persistence can come
later after the comparison format is stable.

## Configuration

Realtime already uses:

```bash
REALTIME_PROVIDER=qwen_omni_realtime
```

Add separate switches:

```bash
OPENING_AUDIO_PROVIDER=doubao_tts|qwen
HANDOFF_ASR_TRANSCRIBER=volcengine_file_asr|qwen_file_asr
```

Do not overload `REALTIME_PROVIDER` to control opening or file ASR. A single
global switch would make failures harder to isolate and would increase rollout
risk.

Qwen credential config should use environment variable names only:

```bash
DASHSCOPE_API_KEY=...
QWEN_OPENING_MODEL=...
QWEN_FILE_ASR_MODEL=qwen3-asr-flash-filetrans
```

No real key belongs in TOML, tests, docs, or committed artifacts.

## Testing Strategy

### Unit/Fake Provider Tests

Required before production code for each adapter:

- Qwen opening generator maps a fake provider response into `OpeningAudio`.
- Opening provider switch selects Doubao by default and Qwen only when explicit.
- Qwen file ASR transcriber parses asynchronous task responses into
  `TranscribedAudio`.
- Handoff ASR adapter keeps the existing `/handoff-transcript` contract while
  selecting Qwen internally.
- Billing report builder includes all buckets and marks missing usage as
  `unavailable`.

### Local Probe Tests

Use short local samples:

- One opening text.
- One 30-60 second WAV for file ASR.
- One short realtime probe WAV.

These prove API shape and local estimates. They do not prove final billing.

### Real API Billing Samples

At least one real API call per paid capability is required before claiming a
billing comparison:

- One Qwen opening generation.
- One Qwen file ASR transcription.
- One Qwen realtime audio probe.
- Equivalent Doubao/Volcengine samples where credentials are available.

After the calls, compare local report totals with the provider console bill.
Console bills can lag; record the checked time and whether the bill has posted.

### Real Phone Validation

Real phone testing is not required for writing code, but is required before
claiming full production readiness. Minimum live check:

- One outbound call with Qwen realtime enabled.
- One outbound call with Qwen opening enabled.
- One call that produces handoff or recording ASR evidence if file ASR is in
  scope for that flow.

The live checklist must verify opening playback, AI response, transcripts,
recording/ASR result, and callback status separately.

## Rollout Safety

- Defaults remain Doubao/Volcengine.
- Qwen switches are opt-in and independent.
- No online `.env` update, restart, or real call is part of implementation
  unless explicitly requested.
- Artifacts go under `artifacts/` and remain gitignored.
- The first deploy should be a no-op deploy with all Qwen switches off.

## Non-Goals

- Do not remove Doubao/Volcengine code in this experiment.
- Do not change Java callback contracts.
- Do not change the handoff HTTP request/response schema.
- Do not store provider API keys in source-controlled config.
- Do not claim billing parity from price tables alone.

## Open Verification Items

- Confirm the chosen Qwen opening API supports the required output format and
  voice in the Beijing region.
- Confirm whether Qwen file ASR should use SDK local upload or OSS URL in the
  first production-like sample.
- Refresh official price constants immediately before real billing comparison.
