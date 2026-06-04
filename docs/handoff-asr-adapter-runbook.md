# 转人工 ASR Adapter 运行说明

## 定位

`app.handoff_asr_adapter` 是转人工阶段的独立 HTTP ASR adapter。

网关在人工通话挂断、双路临时录音停止后，会把以下 JSON POST 到
`HUMAN_TRANSCRIPT_HTTP_URL`：

```json
{
  "call_id": "客户侧 FreeSWITCH channel UUID",
  "context": { "callId": "业务 call_record.id" },
  "agent_id": "agent-1001",
  "agent_uuid": "坐席侧 FreeSWITCH channel UUID",
  "customer_recording_path": "/tmp/call-customer.wav",
  "agent_recording_path": "/tmp/call-agent.wav"
}
```

ASR adapter 读取两路 WAV，返回网关已支持的 turns 合同：

```json
{
  "turns": [
    {
      "role": "assistant",
      "speaker_type": "human_agent",
      "agent_id": "agent-1001",
      "text": "您好，我是物业客服。"
    },
    {
      "role": "user",
      "speaker_type": "customer",
      "text": "我想确认一下费用。"
    }
  ]
}
```

不要返回 `role: "human"`。坐席侧仍使用 `role: "assistant"`，通过
`speaker_type: "human_agent"` 区分真人坐席。

第一版 adapter 按通道输出转写结果：坐席侧一段、客户侧一段。它不会在没有时间戳的情况下
猜测多轮对话的精确交错顺序。后续如果需要句级 turns 或严格时间顺序，需要 ASR provider 返回
分句时间戳，再按时间合并。

## 本地启动

先确保 `.env` 中已有豆包 S2S 凭证：

```bash
DOUBAO_S2S_APP_ID=...
DOUBAO_S2S_ACCESS_TOKEN=...
DOUBAO_S2S_APP_KEY=...
```

启动 adapter：

```bash
uv run python -m app.handoff_asr_adapter \
  --env-file .env \
  --host 127.0.0.1 \
  --port 9200 \
  --send-delay-ms 0
```

这是录音文件转写服务，不是实时通话流，`--send-delay-ms 0` 会尽快发送 WAV
帧，避免两路录音顺序转写时超过网关 HTTP timeout。

网关侧配置：

```toml
[features]
recording_enabled = true
recording_dir = "/var/lib/freeswitch/recordings/handoff"
recording_host_dir = "./freeswitch-local/recordings/handoff"

[human_transcript]
enabled = true
provider = "http_json"
http_url = "http://127.0.0.1:9200/handoff-transcript"
timeout_seconds = 60.0
```

也可以用环境变量覆盖：

```bash
RECORDING_ENABLED=true
RECORDING_DIR=/var/lib/freeswitch/recordings/handoff
RECORDING_HOST_DIR=./freeswitch-local/recordings/handoff
HUMAN_TRANSCRIPT_ENABLED=true
HUMAN_TRANSCRIPT_PROVIDER=http_json
HUMAN_TRANSCRIPT_HTTP_URL=http://127.0.0.1:9200/handoff-transcript
HUMAN_TRANSCRIPT_TIMEOUT_SECONDS=60
```

## 线上部署边界

ASR adapter 必须部署在能读取 `customer_recording_path` 和
`agent_recording_path` 的环境里。第一版推荐与 Python 网关同机部署，或同容器组共享
`features.recording_dir` 挂载。若 FreeSWITCH 在 Docker 容器里运行而网关和 adapter
在宿主机运行，`recording_dir` 应填写容器内路径，`recording_host_dir` 应填写宿主机可读
的同一挂载目录。

不要把 adapter 部署到读不到录音目录的 Java 服务环境。当前合同传的是录音路径，不是音频
bytes。

## 验收

1. `GET /ready` 显示 `human_transcript.enabled=true`，并且
   `features.recording_enabled=true`。
2. 完成一次 AI 外呼转人工，坐席 WebRTC 接通并桥接成功。
3. 挂断后 `handoff.recording_status=completed`。
4. ASR 成功后 `handoff.human_transcript_status=completed`。
5. `call_record.transcript.turns` 同时包含 AI、客户和人工坐席 turns。
6. Java callback 在完整 transcript 写入之后收到 `SUCCESS`。

ASR 失败时，网关应把 `handoff.human_transcript_status` 标记为 `failed`，
并向 Java callback 发送 `FAILED`。
