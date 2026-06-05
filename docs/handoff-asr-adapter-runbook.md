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
      "text": "您好，我是物业客服。",
      "start_ms": 1200,
      "end_ms": 2600,
      "confidence": 0.92
    },
    {
      "role": "user",
      "speaker_type": "customer",
      "text": "我想确认一下费用。",
      "start_ms": 3100,
      "end_ms": 5100,
      "confidence": 0.89
    }
  ]
}
```

不要返回 `role: "human"`。坐席侧仍使用 `role: "assistant"`，通过
`speaker_type: "human_agent"` 区分真人坐席。

adapter 默认分别识别坐席侧和客户侧 WAV。如果 ASR 返回分句时间戳，adapter 会按
`start_ms` 合并双路 turns，尽量还原人工通话顺序；如果 provider 只返回整段文本，
则退化为每路一条 turn，不猜测精确交错顺序。

这两路 WAV 必须是单向录音：`customer.wav` 只录客户侧 channel 的
read/input 音频，`agent.wav` 只录坐席侧 channel 的 read/input 音频。混音录音可以
另作归档或质检，但不适合作为 `customer` / `human_agent` 角色级 transcript 主链路。

## 本地启动

先确保 `.env` 中已有录音文件识别 2.0 凭证。新版推荐只配置 `X-Api-Key`：

```bash
DOUBAO_FILE_ASR_API_KEY=...
DOUBAO_FILE_ASR_RESOURCE_ID=volc.seedasr.auc
```

`DOUBAO_FILE_ASR_RESOURCE_ID` 不配置时默认使用标准版资源
`volc.seedasr.auc`。旧版鉴权仍可用
`DOUBAO_FILE_ASR_APP_KEY` + `DOUBAO_FILE_ASR_ACCESS_KEY` 兜底，但不要复用
`DOUBAO_S2S_*`，那是实时对话链路的凭证。

启动 adapter：

```bash
uv run python -m app.handoff_asr_adapter \
  --env-file .env \
  --host 127.0.0.1 \
  --port 9200 \
  --poll-interval-seconds 2 \
  --max-poll-attempts 60
```

这是录音文件转写服务：adapter 读取挂断后的本地 WAV，通过火山录音文件识别
2.0 标准版 submit/query 接口转写，不再把录音文件伪装成实时 S2S 音频流。

当前主方案是双 mono：客户侧和坐席侧 WAV 分别提交给火山识别，再按
`start_ms` 合并 turns。这个方案多一次 ASR 请求，但角色边界最清楚，优先保证准确率。

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
timeout_seconds = 180.0
```

也可以用环境变量覆盖：

```bash
RECORDING_ENABLED=true
RECORDING_DIR=/var/lib/freeswitch/recordings/handoff
RECORDING_HOST_DIR=./freeswitch-local/recordings/handoff
HUMAN_TRANSCRIPT_ENABLED=true
HUMAN_TRANSCRIPT_PROVIDER=http_json
HUMAN_TRANSCRIPT_HTTP_URL=http://127.0.0.1:9200/handoff-transcript
HUMAN_TRANSCRIPT_TIMEOUT_SECONDS=180
```

标准版是提交后轮询结果。双 mono 模式下两路 WAV 会分别识别。真实超时时间应按
录音长度、provider 返回速度和并发量压测后再收紧。小流量联调阶段先给网关
HTTP adapter 更宽的 `timeout_seconds`，避免把正常排队或转写中的请求误判为失败。

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
