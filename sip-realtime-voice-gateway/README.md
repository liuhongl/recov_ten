# SIP Realtime Voice Gateway

独立的 SIP 实时语音网关项目骨架。

本项目目标是作为实时电话智能客服主链路，不依赖 TEN 框架。当前已经具备：

- 配置文件加载。
- 环境变量覆盖。
- 日志初始化。
- HTTP 健康检查。
- 基础单元测试。
- FreeSWITCH 媒体 WebSocket 回声服务，用于第二阶段闭环验证。
- 音频格式转换模块，用于第三阶段验证 PCM 重采样、分帧和 PCMA 编解码。
- Qwen-Omni-Realtime 离线探测命令，用于第四阶段验证实时模型接入。
- FreeSWITCH 到 Qwen-Omni-Realtime 的电话热链路服务，用于第五阶段验证端到端电话语音闭环。

默认启动仍然是 echo 模式；第五阶段需要显式使用 `--media-mode realtime`。

## 本地启动

```powershell
cd sip-realtime-voice-gateway
python -m app.main --config configs/local.example.toml
```

默认会同时启动：

- HTTP 健康检查：`http://127.0.0.1:9100`
- FreeSWITCH 媒体回声：`ws://0.0.0.0:9101/media/fs/{call_id}`

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:9100/health
```

配置检查：

```powershell
python -m app.main --config configs/local.example.toml --check-config
```

运行测试：

```powershell
python -m pytest
```

## 第二阶段本地电话测试

当前本地 FreeSWITCH 的 `9199` 分机应配置为连接：

```text
ws://host.docker.internal:9101/media/fs/fs_stage5a_local
```

因此测试第二阶段时直接启动本项目，然后用 MicroSIP 拨打 `9199`：

```powershell
cd <repo>\sip-realtime-voice-gateway
python -m app.main --config configs/local.example.toml
```

说话后如果能听到自己的回声，就说明：

```text
MicroSIP -> FreeSWITCH -> sip-realtime-voice-gateway echo -> FreeSWITCH -> MicroSIP
```

这条媒体闭环已经打通。

## 第三阶段音频转换测试

离线测试：

```powershell
cd <repo>\sip-realtime-voice-gateway
python -m pytest tests/test_audio_codec.py
```

电话链路重采样回声测试：

```powershell
cd <repo>\sip-realtime-voice-gateway
$env:FREESWITCH_ECHO_MODE = "resample_16k_roundtrip"
python -m app.main --config configs/local.example.toml
```

然后用 MicroSIP 拨打 `9199`。如果说话后仍能听到自己的回声，说明电话侧 `8k PCM` 经过 `8k -> 16k -> 8k` 后仍能被 FreeSWITCH 正常播放。

## 第四阶段实时模型离线测试

```powershell
cd <repo>\sip-realtime-voice-gateway
python -m app.realtime_probe `
  --config configs/local.example.toml `
  --env-file ../ai_agents/.env `
  --input-pcm ../ai_agents/agents/integration_tests/asr_guarder/tests/test_data/16k_zh_cn.pcm `
  --output-dir artifacts/stage4 `
  --timeout 90
```

也可以直接使用本地 WAV，命令会自动转换为模型需要的 `16kHz mono pcm_s16le`：

```powershell
python -m app.realtime_probe `
  --config configs/local.example.toml `
  --env-file ../ai_agents/.env `
  --input-wav <wav-file> `
  --output-dir artifacts/stage4-user-wav `
  --timeout 120
```

输出：

```text
artifacts/stage4/realtime_output_24k.pcm
artifacts/stage4/realtime_output_24k.wav
artifacts/stage4/realtime_probe_summary.json
```

`artifacts/` 不提交到 git。

## 第五阶段电话端实时语音闭环

阶段 5 使用 realtime 模式：

```powershell
cd <repo>\sip-realtime-voice-gateway
python -m app.main `
  --config configs/local.example.toml `
  --env-file ../ai_agents/.env `
  --media-mode realtime
```

链路：

```text
MicroSIP
  -> FreeSWITCH 9199
  -> sip-realtime-voice-gateway
  -> 8k PCM 转 16k PCM
  -> Qwen-Omni-Realtime
  -> 24k PCM 转 8k PCM
  -> 20ms / 320 bytes 播放队列
  -> FreeSWITCH
  -> MicroSIP
```

当前阶段主要验证用户说完一句话后，电话端能听到 AI 回复。播放中插话只做最小护栏，完整打断能力属于第六阶段。

首轮人工测试发现，AI 播放期间继续说话会导致旧回复尾音和新回复混播。当前已加入最小打断护栏：检测到用户开口后清空本地播放队列，并取消本地 turn task。完整的 `turn_id` 隔离、模型取消事件和迟到音频丢弃仍属于第六阶段。

## 第六阶段持久会话与完整打断控制

阶段 6 仍使用同一启动命令：

```powershell
cd <repo>\sip-realtime-voice-gateway
python -m app.main `
  --config configs/local.example.toml `
  --env-file ../ai_agents/.env `
  --media-mode realtime
```

阶段 6 的主要变化：

- 每通电话只建立一个 Qwen-Omni-Realtime WebSocket session。
- 用户说话期间持续发送 `input_audio_buffer.append`。
- VAD 判定结束后发送 `input_audio_buffer.commit` 和 `response.create`。
- AI 生成或播放期间用户开口时，发送 `response.cancel`，清空本地播放队列，并让旧 `turn_id` 失效。
- 默认 `end_silence_ms` 从 `800ms` 调到 `500ms`，减少本地断句等待。
- 默认 `barge_in_enabled=false`，避免 MicroSIP 外放回采导致 AI 自己触发打断。使用耳机或真实电话回声消除链路时，可设置 `VAD_BARGE_IN_ENABLED=true` 复测完整打断。

详见 `docs/阶段6测试说明.md`。

## 阿里 Server VAD 商用路径

阶段 6 的人工测试说明：继续在“本地 VAD / Manual mode”上补丁式优化，不适合作为商用电话智能客服主线。后续改走阿里官方更适合语音通话的 Server VAD 路线：

```text
FreeSWITCH 持续送 8k PCM
-> Gateway 转 16k PCM 后持续 append 给阿里
-> 阿里 Server VAD 判断 speech_started / speech_stopped
-> Gateway 按 response_id 播放模型音频
-> speech_started 时 cancel 当前 response 并清空播放队列
```

该方式仍然支持最终 SIP 接入。SIP/RTP/PCMA 继续由 FreeSWITCH 负责，Server VAD 只影响 Gateway 和阿里 realtime 之间的对话轮次控制。

后续分阶段：

```text
A1 Server VAD 离线事件流验证
A2 电话持续 append + Server VAD 回复
A3 response_id 下行隔离 + jitter buffer
A4 官方打断 barge-in
A5 商用护栏
```

详见 `docs/阿里ServerVAD商用路径说明.md`。

### A1 离线事件流验证

A1 已验证通过。测试命令：

```powershell
cd <repo>\sip-realtime-voice-gateway
python -m app.server_vad_probe `
  --config configs/local.example.toml `
  --env-file ../ai_agents/.env `
  --input-pcm ../ai_agents/agents/integration_tests/asr_guarder/tests/test_data/16k_zh_cn.pcm `
  --output-dir artifacts/stage-a1-server-vad-v2 `
  --timeout 90
```

长句或朗读样本可先放宽服务端静音窗口：

```powershell
python -m app.server_vad_probe `
  --config configs/local.example.toml `
  --env-file ../ai_agents/.env `
  --input-wav <wav-file> `
  --output-dir artifacts/stage-a1-server-vad-user-wav-silence2000 `
  --timeout 120 `
  --silence-duration-ms 2000 `
  --trailing-silence-ms 3000
```

详见 `docs/阶段A1测试说明.md`。
