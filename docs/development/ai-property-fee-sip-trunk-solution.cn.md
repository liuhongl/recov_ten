# AI 外呼催收物业费 SIP Trunk 技术方案

生成日期：2026-05-08

## 1. 目标与边界

本文描述基于 TEN Framework、FreeSWITCH、SIP trunk 和国产化
ASR/LLM/TTS 的 AI 外呼催收物业费 MVP 技术方案。

业务目标：

1. 通过第三方公网 SIP trunk 外呼业主手机号。
2. 电话接通后，由 AI 完成物业费催缴对话。
3. MVP 阶段支持 10 路并发。
4. 后期可扩展到批量外呼、50 路并发、录音质检、CRM 回写和本地化模型。

当前已确认约束：

1. 第三方 SIP trunk 为 IP 白名单直连，不需要 REGISTER 账号密码。
2. 当前只做外呼，不做呼入。
3. FreeSWITCH 已验证可以打通 SIP 外呼。
4. MVP 先使用 `mod_audio_stream v1.0.3`，按 10 并发以内设计。
5. 国内 SIP trunk 线路编码优先按 PCMA/G.711 A-law、8 kHz、mono 设计。
6. 国产化链路参考 `docs/development/tts-llm-asr-localization.cn.md`。

不在 MVP 范围内：

1. 批量任务调度。
2. CRM 或物业系统回写。
3. 录音质检。
4. 转人工。
5. 多租户。
6. 多台 Call Gateway 横向扩容。
7. 50 并发授权和压测。

## 2. 依据来源与事实边界

本方案分为三类依据。

### 2.1 TEN 官方依据

TEN 官方文档说明了以下基础能力：

1. TEN 使用 graph、node、connection 编排实时语音 Agent。
2. TEN extension 支持 `audio_frame`、`data`、`cmd` 等消息类型。
3. ASR、LLM、TTS、RTC 等模块可以通过 extension 组合。
4. TEN 官方电话示例展示了电话媒体通过 WebSocket 接入 TEN，再经
   ASR/LLM/TTS 回到电话侧的模式。

参考：

- https://theten.ai/cn
- https://theten.ai/cn/docs/ten_agent_examples/overview
- https://theten.ai/cn/docs/ten_agent_examples/project_structure/property_json
- https://theten.ai/cn/docs/ten_agent_examples/project_structure/extension
- https://theten.ai/article/ai-phone-calls-twilio-ten-framework/

### 2.2 当前仓库事实

当前仓库中真实存在的相关内容：

1. `ai_agents/agents/examples/voice-assistant-sip-twilio`
2. `ai_agents/agents/examples/voice-assistant-sip-telnyx`
3. `ai_agents/agents/examples/voice-assistant-sip-plivo`
4. `ai_agents/agents/examples/voice-assistant/tenapp/property.json`
   中已有 `voice_assistant_cn_aliyun` graph。
5. `ai_agents/agents/examples/voice-assistant/tenapp/manifest.json`
   已包含 `aliyun_asr_bigmodel_python` 和 `cosy_tts_python`。
6. `docs/development/tts-llm-asr-localization.cn.md` 明确推荐
   `aliyun_asr_bigmodel_python`、`openai_llm2_python`、`cosy_tts_python`
   组成国产化链路。

### 2.3 项目自定义适配

以下部分是本项目为了公网 SIP trunk 外呼场景新增的工程适配，不是 TEN
官方直接提供的标准方案：

1. FreeSWITCH 对接第三方公网 SIP trunk。
2. Call Gateway 统一外呼控制、状态管理和并发控制。
3. Media Hub 配对 FreeSWITCH 媒体 WebSocket 与 TEN worker。
4. `sip_media_bridge` extension 将 WebSocket PCM 转换为 TEN `AudioFrame`。
5. `mod_audio_stream` 用作 FreeSWITCH 到外部 WebSocket 的双向媒体桥。

## 3. 总体架构

```text
业务系统 / 运营后台
  -> Call Gateway
  -> FreeSWITCH ESL
  -> 第三方 SIP trunk
  -> 业主电话

业主语音
  -> SIP RTP: PCMA/G.711A 8k mono
  -> FreeSWITCH 解码为 PCM
  -> mod_audio_stream
  -> Media Hub
  -> sip_media_bridge
  -> aliyun_asr_bigmodel_python
  -> main_control
  -> openai_llm2_python + 百炼 OpenAI-compatible endpoint
  -> cosy_tts_python
  -> sip_media_bridge
  -> Media Hub
  -> mod_audio_stream
  -> FreeSWITCH 编码为 PCMA
  -> 业主电话
```

一句话边界：

```text
FreeSWITCH 负责电话世界，TEN 负责 AI 世界，Call Gateway 和 Media Hub
负责把两边接起来。
```

## 4. 音频口径

线路侧和 AI 侧必须明确区分。

| 层级 | 格式 |
| --- | --- |
| SIP trunk 线路侧 | PCMA/G.711 A-law, 8 kHz, mono |
| FreeSWITCH 解码后 | linear PCM, 8 kHz, mono |
| Media Hub 与 TEN 输入 | signed 16-bit PCM, 8 kHz, mono |
| ASR 输入 | `pcm_frame`, 8 kHz, mono |
| TTS 输出 | 优先 16 kHz PCM |
| 回电话前 | `sip_media_bridge` 下采样到 8 kHz PCM |
| FreeSWITCH 回线路 | 编码为 PCMA/G.711A |

设计原则：

1. 线路侧优先按 PCMA/G.711A 设计。
2. TEN 不处理 PCMA，只处理解码后的 PCM。
3. 用户上行音频保持 8 kHz，避免无意义升采样。
4. Cosy TTS 先保留 16 kHz 输出，再在桥接层降采样到 8 kHz。
5. 如果后续实测 Cosy TTS 8 kHz 输出稳定，可改成全链路 8 kHz。

## 5. 模块设计

### 5.1 FreeSWITCH

职责：

1. 对接第三方 SIP trunk。
2. 使用 PCMA/G.711A 完成 RTP 编解码。
3. 通过 ESL 接收外呼、挂断、媒体启动命令。
4. 在接通后通过 `mod_audio_stream` 把音频桥接到 Media Hub。

MVP originate 模板：

```text
originate {origination_uuid=<fs_uuid>,originate_timeout=30,ignore_early_media=true,absolute_codec_string=PCMA}sofia/gateway/<gateway_name>/<phone_number> &park()
```

接通后启动媒体：

```text
uuid_audio_stream <fs_uuid> start ws://127.0.0.1:9000/media/fs/<call_id> mono 8k <metadata>
```

`metadata` 示例：

```json
{
  "call_id": "call_abc123",
  "fs_uuid": "<fs_uuid>",
  "trunk_codec": "PCMA",
  "bridge_sample_rate": 8000
}
```

### 5.2 Call Gateway

Call Gateway 是外呼控制面，不处理音频。

MVP 职责：

1. 提供外呼 API。
2. 生成 `call_id` 和 `fs_uuid`。
3. 控制最大并发，MVP 为 10。
4. 调 TEN `/start` 创建 AI 会话。
5. 通过 ESL 调 FreeSWITCH originate。
6. 监听 FreeSWITCH 接通、失败、挂断事件。
7. 接通后启动 `uuid_audio_stream`。
8. 通话结束后调 TEN `/stop` 并释放并发。

MVP 必须内置真实外呼安全能力：

```bash
REAL_CALL_ENABLED=false
ALLOWED_TEST_NUMBERS=
DAILY_CALL_LIMIT_PER_NUMBER=3
CALL_LIMIT_STORE=/tmp/call_gateway_limits.json
```

真实外呼必须同时满足：

1. `REAL_CALL_ENABLED=true`。
2. 请求体中 `real_call_confirm=true`。
3. 如果配置了 `ALLOWED_TEST_NUMBERS`，被叫号码必须在白名单中。
4. 单号码当日外呼次数未超过 `DAILY_CALL_LIMIT_PER_NUMBER`。

MVP 接口：

```http
POST /api/calls
GET /api/calls/{call_id}
DELETE /api/calls/{call_id}
GET /api/calls
GET /health
POST /internal/events
```

`POST /api/calls` 请求示例：

```json
{
  "phone_number": "13800138000",
  "caller": "037123124810",
  "gateway": "sip-provider",
  "owner_name": "张三",
  "community_name": "幸福小区",
  "room_no": "1栋1001",
  "amount_due": 1280.5,
  "due_period": "2025年10月至2026年3月",
  "prompt": "本通电话目标是确认物业费缴纳计划。",
  "greeting": "您好，我是幸福小区物业服务中心智能客服。",
  "voice": "loongluna_v2",
  "callback_url": "https://example.com/call-result",
  "user_data": {
    "owner_id": "owner_001",
    "bill_id": "bill_001"
  },
  "real_call_confirm": true
}
```

返回示例：

```json
{
  "call_id": "call_abc123",
  "status": "dialing"
}
```

MVP 状态：

```text
created
starting_ai
dialing
answered
media_connecting
media_connected
ai_talking
completed
failed
cancelled
```

失败原因：

```text
ten_failed
dial_failed
busy
no_answer
rejected
media_failed
unknown
```

### 5.3 Media Hub

Media Hub 放在 Call Gateway 服务内，负责音频 WebSocket 配对和转发。

建议提供音频调试能力：

```bash
AUDIO_DUMP_ENABLED=false
AUDIO_DUMP_DIR=/tmp/sip_media_dumps
```

建议 dump 产物：

```text
{call_id}.pcm    # 原始上行 8 kHz PCM
{call_id}.jsonl  # 每帧时间、大小、来源、序号
```

建议调试接口：

```http
GET /dumps
WS /replay/{dump_name}
WS /simulate
```

这些接口只用于开发和联调，生产环境必须加鉴权或关闭。

WebSocket 地址：

```text
FreeSWITCH: ws://127.0.0.1:9000/media/fs/{call_id}
TEN:        ws://127.0.0.1:9000/media/ten/{call_id}
```

配对规则：

```text
同一个 call_id 下 fs_ws 和 ten_ws 都连接成功，状态为 media_connected。
```

配对成功后，Media Hub 向 TEN 侧发送控制消息：

```json
{
  "type": "control",
  "event": "media_connected",
  "call_id": "call_abc123"
}
```

音频流：

```text
FreeSWITCH -> Media Hub -> TEN:
8 kHz mono PCM binary

TEN -> Media Hub:
8 kHz mono PCM binary

Media Hub -> FreeSWITCH:
streamAudio JSON + base64 raw PCM
```

回放消息：

```json
{
  "type": "streamAudio",
  "data": {
    "audioDataType": "raw",
    "sampleRate": 8000,
    "audioData": "<base64 raw pcm>"
  }
}
```

### 5.4 sip_media_bridge extension

`sip_media_bridge` 是 TEN worker 内部的电话媒体适配 extension。

职责：

1. 启动时根据 `channel` 连接 Media Hub。
2. 收到 Media Hub binary PCM 后创建 TEN `AudioFrame("pcm_frame")`。
3. 把 `pcm_frame` 发给 ASR。
4. 收到 TTS 的 `pcm_frame` 后，必要时从 16 kHz 降采样到 8 kHz。
5. 将 8 kHz PCM binary 发回 Media Hub。
6. 收到 `media_connected` 后通知 `main_control` 发开场白。

`property.json` 节点示例：

```json
{
  "type": "extension",
  "name": "sip_media_bridge",
  "addon": "sip_media_bridge",
  "extension_group": "default",
  "property": {
    "channel": "default",
    "media_hub_base_url": "${env:MEDIA_HUB_BASE_URL|ws://127.0.0.1:9000}",
    "sample_rate": 8000
  }
}
```

这里利用 TEN server 的 channel 注入能力：Call Gateway 调 `/start` 时传
`channel_name = call_id`，TEN 会把 `sip_media_bridge.channel` 注入为
实际 `call_id`。

### 5.5 main_control

`main_control` 负责催收对话业务逻辑。

MVP 对话状态：

```text
waiting_identity
explaining_debt
handling_objection
collecting_commitment
wrapping_up
finished
```

基本流程：

```text
media_connected
  -> AI 开场
  -> 身份确认
  -> 说明欠费
  -> 处理异议或承诺
  -> 总结
  -> conversation_done
```

话术边界：

1. 必须先确认接听人身份，再披露欠费明细。
2. 不威胁、不恐吓、不辱骂。
3. 不承诺未经授权的减免、法律后果或强制措施。
4. 不确定的问题引导联系物业人工。
5. 每轮回复简短、口语化，适合电话播放。

动态字段优先级：

```text
请求中的 greeting > property.json 默认 greeting
请求中的 prompt > property.json 默认 prompt
请求中的 voice > .env 默认 voice
```

Call Gateway 调 TEN `/start` 时，应把 `prompt`、`greeting`、`callback_url`、
`user_data` 等字段放入 `properties.main_control.call_context`，并把 `voice`
写入 `properties.tts.params.voice`。

通话结果示例：

```json
{
  "call_id": "call_abc123",
  "identity_confirmed": true,
  "result": "promise_to_pay",
  "promise_date": "2026-05-12",
  "objection_type": null,
  "need_human_followup": false,
  "summary": "用户确认本人，表示本周内缴纳物业费。"
}
```

结果枚举：

```text
promise_to_pay
already_paid
fee_dispute
not_owner
refused
no_clear_result
need_human_followup
```

## 6. TEN Graph 设计

新增 graph：

```text
voice_assistant_sip_trunk_cn_aliyun
```

命名含义：

1. `sip_trunk`：公网 SIP trunk 外呼场景。
2. `cn`：中文电话业务。
3. `aliyun`：默认使用阿里百炼国产化链路。

节点：

```text
sip_media_bridge
stt: aliyun_asr_bigmodel_python
llm: openai_llm2_python
tts: cosy_tts_python
main_control
message_collector
```

核心连接：

```text
sip_media_bridge pcm_frame -> stt
stt asr_result -> main_control
main_control text_data -> llm
llm text_data -> main_control
main_control tts_text_input -> tts
tts pcm_frame -> sip_media_bridge
```

ASR 节点：

```json
{
  "type": "extension",
  "name": "stt",
  "addon": "aliyun_asr_bigmodel_python",
  "extension_group": "stt",
  "property": {
    "params": {
      "api_key": "${env:ALIYUN_ASR_BIGMODEL_API_KEY}",
      "model": "paraformer-realtime-v2",
      "sample_rate": 8000,
      "language_hints": ["zh", "en"],
      "punctuation_prediction_enabled": true,
      "inverse_text_normalization_enabled": true,
      "max_sentence_silence": 600
    }
  }
}
```

LLM 节点：

```json
{
  "type": "extension",
  "name": "llm",
  "addon": "openai_llm2_python",
  "extension_group": "chatgpt",
  "property": {
    "base_url": "${env:LLM_BASE_URL}",
    "api_key": "${env:LLM_API_KEY}",
    "model": "${env:LLM_MODEL}",
    "max_tokens": 512,
    "frequency_penalty": 0.3,
    "prompt": "你是物业费外呼催收智能客服。你必须先确认接听人身份，再说明欠费信息。回复要自然、简短、礼貌，适合电话说出来。不能威胁、恐吓、辱骂，不能承诺未经授权的减免或法律后果。",
    "max_memory_length": 10
  }
}
```

TTS 节点：

```json
{
  "type": "extension",
  "name": "tts",
  "addon": "cosy_tts_python",
  "extension_group": "tts",
  "property": {
    "dump": false,
    "dump_path": "./",
    "params": {
      "api_key": "${env:COSY_TTS_API_KEY}",
      "model": "${env:COSY_TTS_MODEL|cosyvoice-v3}",
      "sample_rate": 16000,
      "voice": "${env:COSY_TTS_VOICE|loongluna_v2}"
    }
  }
}
```

## 7. Call Gateway 调 TEN /start

Call Gateway 发起一通外呼时，应先启动 TEN worker。

请求示例：

```json
{
  "request_id": "call_abc123",
  "channel_name": "call_abc123",
  "graph_name": "voice_assistant_sip_trunk_cn_aliyun",
  "properties": {
    "main_control": {
      "call_context": {
        "call_id": "call_abc123",
        "owner_name": "张三",
        "community_name": "幸福小区",
        "room_no": "1栋1001",
        "amount_due": 1280.5,
        "due_period": "2025年10月至2026年3月",
        "prompt": "本通电话目标是确认物业费缴纳计划。",
        "greeting": "您好，我是幸福小区物业服务中心智能客服。",
        "callback_url": "https://example.com/call-result",
        "user_data": {
          "owner_id": "owner_001",
          "bill_id": "bill_001"
        }
      }
    },
    "tts": {
      "params": {
        "voice": "loongluna_v2"
      }
    }
  },
  "timeout": 300
}
```

说明：

1. `channel_name` 使用 `call_id`。
2. `graph_name` 固定为 `voice_assistant_sip_trunk_cn_aliyun`。
3. 催收上下文通过 `properties.main_control.call_context` 注入。
4. `sip_media_bridge.channel` 由 TEN 的 channel 注入机制自动变为 `call_id`。
5. 动态音色通过 `properties.tts.params.voice` 注入。

## 8. 代码结构建议

新增 example：

```text
ai_agents/agents/examples/voice-assistant-sip-trunk/
├── server/
│   ├── main.py
│   ├── config.py
│   ├── schemas.py
│   ├── call_gateway.py
│   ├── freeswitch_esl.py
│   ├── ten_client.py
│   ├── session_store.py
│   └── media_hub.py
└── tenapp/
    ├── property.json
    ├── manifest.json
    └── ten_packages/extension/
        ├── sip_media_bridge/
        │   ├── addon.py
        │   ├── extension.py
        │   ├── config.py
        │   ├── manifest.json
        │   ├── property.json
        │   └── requirements.txt
        └── main_python/
```

MVP 不做前端。使用 HTTP API 或 curl 进行联调。

## 9. TTS 播放策略

主路径使用 `mod_audio_stream v1.0.3` 的双向 `streamAudio` 能力：

```text
TTS PCM -> sip_media_bridge -> Media Hub -> streamAudio -> FreeSWITCH
```

WAV + `uuid_broadcast` 可作为 fallback：

```text
TTS PCM -> WAV chunk -> ESL uuid_broadcast -> FreeSWITCH
```

配置建议：

```bash
TTS_PLAYBACK_MODE=stream_audio
TTS_PLAYBACK_FALLBACK=uuid_broadcast
TTS_PLAYBACK_GAIN=0.75
TTS_CHUNK_MAX_MS=1200
TTS_FLUSH_IDLE_MS=250
TTS_INTERRUPT_ENABLED=true
```

说明：

1. `stream_audio` 是主路径，目标是降低延迟并改善打断粒度。
2. `uuid_broadcast` 仅作为兼容性 fallback。
3. 如果某个 FreeSWITCH 或 `mod_audio_stream` 环境下双向播放不稳定，可以临时切换到 fallback。

## 10. 环境变量

Call Gateway：

```bash
CALL_GATEWAY_HOST=0.0.0.0
CALL_GATEWAY_PORT=9000
CALL_MAX_CONCURRENCY=10

TEN_API_BASE_URL=http://127.0.0.1:8080
TEN_GRAPH_NAME=voice_assistant_sip_trunk_cn_aliyun
TEN_DEFAULT_TIMEOUT=300

FS_ESL_HOST=127.0.0.1
FS_ESL_PORT=8021
FS_ESL_PASSWORD=ClueCon
FS_GATEWAY_NAME=sip_trunk_provider
FS_ORIGINATE_TIMEOUT=30
FS_CODEC=PCMA

MEDIA_HUB_BASE_URL=ws://127.0.0.1:9000
MEDIA_SAMPLE_RATE=8000
TTS_SAMPLE_RATE=16000

REAL_CALL_ENABLED=false
ALLOWED_TEST_NUMBERS=
DAILY_CALL_LIMIT_PER_NUMBER=3
CALL_LIMIT_STORE=/tmp/call_gateway_limits.json

AUDIO_DUMP_ENABLED=false
AUDIO_DUMP_DIR=/tmp/sip_media_dumps

TTS_PLAYBACK_MODE=stream_audio
TTS_PLAYBACK_FALLBACK=uuid_broadcast
TTS_PLAYBACK_GAIN=0.75
TTS_CHUNK_MAX_MS=1200
TTS_FLUSH_IDLE_MS=250
TTS_INTERRUPT_ENABLED=true
```

国产化 AI 链路：

```bash
ALIYUN_ASR_BIGMODEL_API_KEY=your_bailian_key

LLM_API_KEY=your_bailian_key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

COSY_TTS_API_KEY=your_bailian_key
COSY_TTS_MODEL=cosyvoice-v3
COSY_TTS_VOICE=loongluna_v2
```

密钥要求：

1. 所有 key 只写入 `.env`。
2. `property.json` 只使用 `${env:VAR_NAME}`。
3. 日志不得输出原始 key。
4. 音频 dump 默认关闭。

## 11. 部署方案

MVP 推荐单机部署：

```text
公网云主机
├── FreeSWITCH 裸机安装
├── TEN Agent 服务
└── Call Gateway + Media Hub 服务
```

端口建议：

| 端口 | 服务 | 暴露范围 |
| --- | --- | --- |
| 5060/UDP | SIP 信令 | 仅第三方 SIP IP |
| 10000-20000/UDP | RTP | 仅第三方媒体 IP |
| 8021/TCP | FreeSWITCH ESL | 仅本机或内网 |
| 8080/TCP | TEN API | 仅本机或内网 |
| 9000/TCP | Call Gateway / Media Hub | 业务系统或内网 |
| 3000/TCP | Playground | 开发环境可用，生产关闭 |
| 49483/TCP | TMAN Designer | 开发环境可用，生产关闭 |

启动顺序：

```text
1. 启动 FreeSWITCH。
2. 确认 SIP trunk 可外呼。
3. 确认 mod_audio_stream 已加载。
4. 启动 TEN Agent。
5. 确认 TEN /health 可用。
6. 启动 Call Gateway。
7. 调 POST /api/calls 发起测试外呼。
```

检查命令：

```bash
fs_cli -x "status"
fs_cli -x "show api uuid_audio_stream"
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:9000/health
```

## 12. 联调顺序

不要一开始就跑完整 AI 闭环。按以下顺序验证：

1. FreeSWITCH 单独通过 SIP trunk 外呼成功。
2. FreeSWITCH 使用 PCMA 编码协商成功。
3. `mod_audio_stream` 加载成功。
4. Call Gateway 能连接 ESL。
5. `POST /api/calls` 能触发 FreeSWITCH originate。
6. `CHANNEL_ANSWER` 能被 Call Gateway 捕获。
7. 接通后能执行 `uuid_audio_stream start`。
8. Media Hub 收到 `/media/fs/{call_id}` 连接。
9. TEN `/start` 后 `sip_media_bridge` 连接 `/media/ten/{call_id}`。
10. Media Hub 两侧配对，发送 `media_connected`。
11. 电话语音进入 `aliyun_asr_bigmodel_python`。
12. LLM 生成中文短句。
13. Cosy TTS 音频回到电话侧。
14. 用户打断能停止当前 TTS。
15. 用户挂断后 TEN `/stop` 和 Call Gateway 状态清理正常。

## 13. 验收标准

MVP 通过标准：

1. `/graphs` 返回 `voice_assistant_sip_trunk_cn_aliyun`。
2. 单通电话接通后，AI 能播放开场白。
3. 用户中文电话语音能被识别。
4. AI 回复简短、自然、适合电话播放。
5. TTS 音频能通过 FreeSWITCH 回放给用户。
6. 用户插话时能打断当前 TTS。
7. 主动挂断接口能停止 FreeSWITCH 通话和 TEN session。
8. 未接、忙线、拒接等失败能形成明确失败原因。
9. 10 路并发不串线。
10. 日志不泄露手机号明文和密钥。

## 14. 批量外呼扩展

MVP 不实现批量外呼，但代码要保留边界：

```text
create_call()
start_call(call_id)
finish_call(call_id)
```

后期批量外呼新增：

```text
Batch Scheduler
  -> 创建多个 call job
  -> 按并发限制调用 start_call(call_id)
```

新增接口可为：

```http
POST /api/call-batches
GET /api/call-batches/{batch_id}
POST /api/call-batches/{batch_id}/pause
POST /api/call-batches/{batch_id}/resume
POST /api/call-batches/{batch_id}/cancel
```

为了避免 MVP 过度设计，当前不引入数据库、队列或批次调度器。后期需要服务重启不丢任务时，再引入 Redis 或数据库。

## 15. 风险与待验证项

1. `mod_audio_stream v1.0.3` 免费版本按 10 并发以内使用；50 并发需要授权、试用或替代媒体桥。
2. `aliyun_asr_bigmodel_python` 在 8 kHz 电话音频上的识别准确率需要实测。
3. `cosy_tts_python` 16 kHz 输出下采样到 8 kHz 后的电话听感需要实测。
4. FreeSWITCH、Media Hub、TEN 三侧的通话清理必须幂等。
5. `conversation_done` 与 `CHANNEL_HANGUP` 可能同时发生，Call Gateway 需要避免重复清理。
6. 生产环境必须限制 SIP/RTP 来源 IP、ESL 访问范围和内部接口访问范围。
7. 后续接入录音、质检和 CRM 回写前，需要补数据留存、权限审计和脱敏策略。
8. 如果不提供 dump/replay/simulate 能力，音频问题排查会明显变难。
9. WAV + `uuid_broadcast` fallback 不能作为长期低延迟主路径。

## 16. 最终结论

MVP 推荐方案：

```text
FreeSWITCH + mod_audio_stream v1.0.3
  -> Call Gateway + Media Hub
  -> TEN voice_assistant_sip_trunk_cn_aliyun
  -> aliyun_asr_bigmodel_python
  -> openai_llm2_python + qwen-plus
  -> cosy_tts_python
```

该方案遵循以下原则：

1. FreeSWITCH 处理电话和 SIP/RTP。
2. TEN 处理 AI 实时语音链路。
3. Call Gateway 处理外呼控制和生命周期。
4. Media Hub 和 `sip_media_bridge` 只做媒体适配。
5. 国产化 AI 链路沿用当前项目已有扩展和 `voice_assistant_cn_aliyun` 方案。
6. 内置真实外呼保护、dump/replay/simulate 调试能力和动态业务字段。
