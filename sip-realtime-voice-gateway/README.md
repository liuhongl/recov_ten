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

后续阶段才会把实时模型接入电话热链路。

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
