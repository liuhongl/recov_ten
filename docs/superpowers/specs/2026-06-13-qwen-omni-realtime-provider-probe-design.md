# Qwen Omni Realtime Provider Probe Design

## Goal

Add an isolated Qwen-Omni-Realtime experiment path that can compare realtime speech quality and usage cost against the current Doubao S2S path without changing the production default provider.

## Scope

This change adds a standalone Qwen Realtime client, a command-line probe, usage parsing, and RMB cost estimation from `response.done.usage`. It does not switch `realtime_phone_gateway` away from Doubao and does not deploy anything online.

## Architecture

The Qwen client mirrors the existing Doubao probe boundary: one module owns WebSocket protocol details, one CLI loads local credentials and writes audio plus a JSON summary. The first implementation uses WebSocket manual audio mode so probes are deterministic: append PCM16/16k audio, commit the buffer, send `response.create`, then collect `response.audio.delta`, transcription events, and `response.done`.

## Data Flow

1. Read `DASHSCOPE_API_KEY` from local environment or `.env`.
2. Connect to `wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=...` using Bearer auth.
3. Send `session.update` with `["text", "audio"]`, PCM input/output, voice, instructions, and manual turn detection.
4. Stream input WAV as base64 `input_audio_buffer.append` events.
5. Send `input_audio_buffer.commit` and `response.create`.
6. Collect output PCM, input transcript, assistant transcript, sanitized events, latency timings, and provider usage.
7. Estimate cost from official mainland Qwen-Omni-Realtime price table while preserving raw usage for later bill reconciliation.

## Testing

Unit tests use a local fake WebSocket server. They verify request headers, query model, emitted client events, output audio collection, transcript extraction, usage parsing, and cost estimation. Full project tests remain the completion gate.
