# Voice Assistant SIP Trunk

Minimal staged example for connecting a SIP trunk phone call path to TEN.

The current implementation covers stages 1 through 6A and adds the stage 6B-1
LLM A/B graph. Stages 5A and 5B validate local FreeSWITCH phone media through
Media Hub and `sip_media_bridge`. Stage 6A validates the local `9199` minimal
ASR/LLM/TTS phone loop. Stage 6B-1 keeps the same ASR/TTS path and swaps only
the LLM to Aliyun DashScope through the OpenAI-compatible API.

## Stage 1 Graph

```text
voice_assistant_sip_trunk_cn_skeleton
  -> sip_media_bridge
```

`sip_media_bridge` is provided from
`ai_agents/agents/ten_packages/extension/sip_media_bridge`. In the current
code it already contains later-stage Media Hub connection and audio conversion
logic. If this stage is run without Media Hub, the graph still validates the
control path, but the bridge will log Media Hub connection retry warnings.

## Stage 2 Media Hub Control Connection

Stage 2 adds the smallest useful Media Hub control path:

```text
sip_media_bridge -> ws://127.0.0.1:9000/media/ten/{channel}
```

It validates WebSocket connection, registration, heartbeat, and disconnect
cleanup. It still does not transfer PCM audio, connect FreeSWITCH, or run
ASR/LLM/TTS.

Media Hub currently treats the TEN side as ready only after it sends a
`register` control message. The FS side is considered ready after connection,
because the eventual FreeSWITCH media WebSocket may not have a separate
registration message.

If a second connection arrives for the same `channel + role`, Media Hub
replaces the old session, closes the old WebSocket, and keeps the newest
session for that channel.

Run the Media Hub before starting the TEN graph:

```bash
task run-media-hub
```

Expected Media Hub log events:

```text
ten_connected channel=<call_id>
ten_registered channel=<call_id>
heartbeat channel=<call_id>
session_removed channel=<call_id>
```

Expected TEN log events:

```text
sip_media_bridge media hub registered: channel=<call_id>
sip_media_bridge media hub heartbeat acknowledged: channel=<call_id>
```

## Stage 3 Media Hub Fake Audio Stream

Stage 3 validates Media Hub's internal audio forwarding without FreeSWITCH and
without TEN AudioFrame conversion. It uses two fake clients:

```text
fake_fs_client -> /media/fs/{call_id}
Media Hub forwards binary PCM bytes
fake_ten_client -> /media/ten/{call_id}
fake_ten_client echoes bytes back
Media Hub forwards binary PCM bytes
fake_fs_client receives the echo
```

Run Media Hub first, then run the fake audio test:

```bash
task run-media-hub
task test-media-hub-fake-audio
```

Expected Media Hub log events:

```text
fs_connected channel=<call_id>
media_paired channel=<call_id>
audio_forwarded direction=fs_to_ten channel=<call_id>
audio_forwarded direction=ten_to_fs channel=<call_id>
```

Expected fake client result:

```text
fake_audio_roundtrip_ok channel=<call_id>
```

## Stage 4 TEN AudioFrame Roundtrip

Stage 4 validates the `sip_media_bridge` audio conversion path inside TEN:

```text
fake_fs_client -> Media Hub binary PCM
Media Hub -> sip_media_bridge
sip_media_bridge -> TEN AudioFrame("pcm_frame")
sip_media_bridge_test_echo -> TEN AudioFrame("pcm_frame")
sip_media_bridge -> Media Hub binary PCM
Media Hub -> fake_fs_client
```

Use graph `voice_assistant_sip_trunk_audio_frame_test`.

Run Media Hub and the TEN API server, start the stage 4 graph with channel
`call_stage4_audio_frame_001`, then run:

```bash
task test-bridge-audio-frame-roundtrip
```

Expected TEN log events:

```text
sip_media_bridge received_pcm_from_media_hub: channel=<call_id>
sip_media_bridge_test_echo received_audio_frame: bytes=<n>
sip_media_bridge sent_pcm_to_media_hub: channel=<call_id>
```

Expected fake FS result:

```text
fake_fs_audio_roundtrip_ok channel=<call_id>
```

## Stage 5A Local FreeSWITCH Media Hub Echo

Stage 5A validates real local phone media up to Media Hub and back to the
phone side:

```text
MicroSIP -> FreeSWITCH 9199
FreeSWITCH mod_audio_stream -> Media Hub /media/fs/fs_stage5a_local
Media Hub -> fake_ten_audio_responder /media/ten/fs_stage5a_local
fake_ten_audio_responder echoes PCM bytes
Media Hub -> FreeSWITCH mod_audio_stream
FreeSWITCH -> MicroSIP
```

The local FreeSWITCH container must provide `uuid_audio_stream`. In the current
local test environment this is done by a git-ignored derived image under
`ai_agents/local/freeswitch/image` with `mod_audio_stream` v1.0.3.

Run Media Hub and the fake TEN echo responder inside `ten_agent_dev`, then
dial `9199` from MicroSIP:

```bash
docker exec -d ten_agent_dev bash -lc "cd /app/agents/examples/voice-assistant-sip-trunk && python3 server/main.py --host 0.0.0.0 --port 9000 --heartbeat-timeout 60 > /tmp/sip_trunk_media_hub_stage5a.log 2>&1"
docker exec -d ten_agent_dev bash -lc "cd /app/agents/examples/voice-assistant-sip-trunk && python3 server/fake_ten_audio_responder.py --url ws://127.0.0.1:9000 --channel fs_stage5a_local --mode echo --timeout 180 --max-inbound-chunks 1000 > /tmp/fake_ten_audio_responder_stage5a.log 2>&1"
```

Expected Media Hub log events:

```text
fs_connected channel=fs_stage5a_local
media_paired channel=fs_stage5a_local
audio_forwarded direction=fs_to_ten channel=fs_stage5a_local bytes=320
audio_forwarded direction=ten_to_fs channel=fs_stage5a_local bytes=320
```

Expected fake TEN responder log events:

```text
fs_audio_received chunk=<n> bytes=320
echo_sent chunk=<n> bytes=320
```

This stage still uses a fake TEN responder rather than `sip_media_bridge`.

## Stage 5B Local FreeSWITCH TEN AudioFrame Echo

Stage 5B validates real local phone media through the TEN bridge and
AudioFrame echo graph:

```text
MicroSIP -> FreeSWITCH 9199
FreeSWITCH mod_audio_stream -> Media Hub /media/fs/fs_stage5a_local
Media Hub -> sip_media_bridge /media/ten/fs_stage5a_local
sip_media_bridge -> TEN AudioFrame("pcm_frame")
sip_media_bridge_test_echo -> TEN AudioFrame("pcm_frame")
sip_media_bridge -> Media Hub
Media Hub -> FreeSWITCH mod_audio_stream
FreeSWITCH -> MicroSIP
```

Start Media Hub and the TEN API server inside `ten_agent_dev`, then start graph
`voice_assistant_sip_trunk_audio_frame_test` with
`channel_name=fs_stage5a_local`. Dial `9199` from MicroSIP.

Expected Media Hub log events:

```text
ten_registered channel=fs_stage5a_local sample_rate=8000
fs_connected channel=fs_stage5a_local
media_paired channel=fs_stage5a_local
audio_forwarded direction=fs_to_ten channel=fs_stage5a_local bytes=320
audio_forwarded direction=ten_to_fs channel=fs_stage5a_local bytes=320
```

Expected TEN log events:

```text
sip_media_bridge received_pcm_from_media_hub: channel=fs_stage5a_local, bytes=320, sample_rate=8000
sip_media_bridge_test_echo received_audio_frame: bytes=320, sample_rate=8000, channels=1, bytes_per_sample=2, samples_per_channel=160
sip_media_bridge sent_pcm_to_media_hub: channel=fs_stage5a_local, bytes=320, sample_rate=8000
```

Verified result on 2026-05-08: the phone side heard echo with no obvious
stutter, and FreeSWITCH `show calls` returned `0` after hangup.

## Stage 6A Local FreeSWITCH Minimal AI Loop

Stage 6A validates one local phone sentence through ASR, LLM, TTS, and back to
the phone side:

```text
MicroSIP -> FreeSWITCH 9199
FreeSWITCH mod_audio_stream -> Media Hub /media/fs/fs_stage5a_local
Media Hub -> sip_media_bridge
sip_media_bridge -> Deepgram ASR
ASR final text -> sip_trunk_dialog_controller
sip_trunk_dialog_controller -> DeepSeek/OpenAI-compatible LLM
sip_trunk_dialog_controller -> ElevenLabs TTS
TTS AudioFrame -> sip_media_bridge
sip_media_bridge -> Media Hub
Media Hub -> FreeSWITCH -> MicroSIP
```

Use graph `voice_assistant_sip_trunk_cn_ai_minimal`.

Current 6A configuration:

```text
ASR: Deepgram nova-3, zh-CN, 8000 Hz linear16
LLM: OPENAI_API_BASE=https://api.deepseek.com/v1, model=deepseek-chat
TTS: ElevenLabs eleven_multilingual_v2, output_format=pcm_16000
Bridge output guard: resample/downmix outgoing PCM to 8000 Hz mono s16le
```

Verified result:

```text
sip_trunk_dialog_controller created and started
Deepgram ASR WebSocket opened
LLM initialized with deepseek-chat
Media Hub TEN side registered channel=fs_stage5a_local sample_rate=8000
Phone side heard generated AI reply
```

Measured sample:

```text
User text: 你好呀
AI reply: 你好！有什么可以帮你的吗？
Phone audio entered -> ASR final: about 2.8s
ASR final -> first LLM text: about 2.28s
First LLM text -> first TTS audio: about 0.78s
Phone audio entered -> first audible AI: about 5.88s
```

## Stage 6B-1 Aliyun LLM A/B Graph

Stage 6B-1 isolates LLM latency by keeping ASR and TTS unchanged while swapping
only the LLM provider:

```text
Deepgram ASR + Aliyun DashScope/OpenAI-compatible LLM + ElevenLabs TTS
```

Use graph `voice_assistant_sip_trunk_cn_ai_aliyun_llm`.

Required local environment variables:

```text
ALIYUN_DASHSCOPE_API_KEY=<local secret>
ALIYUN_OPENAI_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
ALIYUN_OPENAI_MODEL=qwen-flash
```

Do not confuse this graph with Aliyun realtime voice models. It still uses the
staged ASR -> LLM -> TTS path and only tests text LLM first-token/first-sentence
latency.

## Media Contract

The validated media contract is:

```text
PCM s16le
8000 Hz
mono
20 ms per frame
320 bytes per frame
```

Any FreeSWITCH deployment used in the next stage should either emit this format
or provide a clear conversion point before audio enters Media Hub.
