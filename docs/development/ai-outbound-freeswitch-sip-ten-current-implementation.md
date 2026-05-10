# AI 外呼 FreeSWITCH + SIP + TEN 当前实现技术文档

生成日期：2026-05-08

本文记录当前项目中 `voice-assistant-sip-freeswitch` 示例的实际实现状态。它不是目标架构设想，而是按当前代码、配置和 README 梳理出的工程文档。

## 1. 当前结论

当前项目已经打通了 AI 外呼的第一版核心闭环：

- FreeSWITCH 负责 SIP trunk、外呼、接听后媒体流转发和 TTS 回放。
- Bridge 负责业务 HTTP API、ESL 控制、音频 WebSocket 转发、真实外呼保护、录音 dump 和回放测试。
- TEN 负责实时语音智能链路：ASR -> LLM -> TTS。
- 当前可以在电话接通后播放开场白。
- 当前可以把用户电话语音送入 TEN，经过 ASR/LLM/TTS 后回到电话侧形成对话。

从第一性原理看，这个系统的本质不是“让 TEN 直接支持 SIP”，而是把电话系统和 AI 系统通过一个媒体桥接层解耦：

```text
SIP/电话世界：信令、号码、接通、挂断、运营商编码
AI/TEN 世界：PCM 音频帧、ASR、LLM、TTS、打断
Bridge：把两边协议、采样率、编码、会话生命周期对齐
```

因此第一版正确边界是：FreeSWITCH 做电话，TEN 做 AI，Bridge 做适配。不要把 SIP 栈塞进 TEN，也不要把 ASR/LLM/TTS 业务逻辑塞进 FreeSWITCH。

## 2. 代码位置

主示例目录：

```text
ai_agents/agents/examples/voice-assistant-sip-freeswitch/
```

核心文件：

| 文件 | 作用 |
|---|---|
| `README.md` | 示例使用说明 |
| `.env.example` | 环境变量模板 |
| `Taskfile.yml` | 安装、运行、构建任务 |
| `server/fs_bridge.py` | FreeSWITCH 与 TEN 的主 Bridge 服务 |
| `server/fs_originate.py` | 外呼命令辅助脚本 |
| `server/static/index.html` | 管理台页面 |
| `freeswitch-config/sip_profile_external.xml` | SIP trunk gateway 示例配置 |
| `freeswitch-config/dialplan_outbound.xml` | AI 外呼 dialplan |
| `freeswitch-config/dialplan_inbound.xml` | AI 来电 dialplan |
| `tenapp/property.json` | TEN 图配置 |
| `tenapp/ten_packages/extension/main_python/extension.py` | 电话场景主控扩展 |
| `tenapp/ten_packages/extension/main_python/server.py` | TEN `/media` WebSocket 服务 |

注意：`main_python` 中仍有 `AsteriskControlExtension`、`AsteriskCallServer`、`asterisk_server_port` 等历史命名。当前 FreeSWITCH 方案复用了这部分 WebSocket 媒体服务能力，命名不代表当前依赖 Asterisk。

## 3. 总体架构

```text
业务系统 / 管理台
  -> HTTP POST /originate
  -> Bridge :9003
  -> FreeSWITCH ESL :8021
  -> FreeSWITCH originate
  -> SIP trunk
  -> 用户电话

用户说话
  -> SIP/RTP
  -> FreeSWITCH
  -> mod_audio_stream
  -> ws://Bridge:9003/audio/{uuid}
  -> Bridge
  -> ws://127.0.0.1:9002/media
  -> TEN main_python
  -> aliyun_asr
  -> openai_llm2_python
  -> cosy_tts_python
  -> TEN main_python
  -> Bridge
  -> ESL uuid_broadcast
  -> FreeSWITCH
  -> 用户电话
```

当前默认端口：

| 端口 | 服务 | 说明 |
|---|---|---|
| `19000/UDP` | FreeSWITCH SIP | 示例里的外部 SIP profile 端口说明 |
| `8021/TCP` | FreeSWITCH ESL | Bridge 通过 ESL 发起外呼、挂断、播放音频 |
| `9002/TCP` | TEN media WebSocket | `main_python` 提供 `/media` |
| `9003/TCP` | FreeSWITCH Bridge | HTTP API、管理台、`/audio/{call_id}` |

## 4. TEN 图配置

当前 `tenapp/property.json` 中的图名是 `voice_assistant`，`auto_start` 为 `true`。

节点：

| 节点 | addon | 当前作用 |
|---|---|---|
| `stt` | `aliyun_asr` | 电话上行音频识别，配置采样率 `16000` |
| `llm` | `openai_llm2_python` | 通过 OpenAI 兼容接口调用大模型，默认 `qwen-plus` |
| `tts` | `cosy_tts_python` | 使用 CosyVoice 生成语音，配置采样率 `16000` |
| `main_control` | `main_python` | 电话媒体入口、开场白、打断、ASR/LLM/TTS 编排 |
| `message_collector` | `message_collector2` | 收集转写和助手消息 |

关键连接：

- `main_control` 发送 `pcm_frame` 到 `stt`。
- `stt` 返回 `asr_result` 到 `main_control`。
- `tts` 返回 `pcm_frame` 到 `main_control`。
- `main_control` 通过 data 消息把文本送给 LLM/TTS，并把转写送给 `message_collector`。

当前默认开场白：

```text
您好，我是智能语音助手，想和您确认一笔还款事项。
```

当前默认 LLM 话术约束强调电话化表达：简短、自然、礼貌、每轮只追问一个问题、不要解释模型身份。

## 5. 外呼链路

业务外呼入口是 Bridge 的：

```http
POST /originate
```

请求模型 `OriginateRequest` 支持：

| 字段 | 说明 |
|---|---|
| `callee` | 被叫号码，必填 |
| `caller` | 主叫号码，默认 `037123124810` |
| `gateway` | FreeSWITCH gateway 名称，默认 `sip-provider` |
| `prompt` | 动态 LLM system prompt，当前会存入 session |
| `greeting` | 动态开场白，当前会存入 session |
| `voice` | TTS 声色，当前会存入 session |
| `callback_url` | 挂断后结果回调地址 |
| `user_data` | 业务透传数据 |
| `encoding` | 音频编码字段，默认 `pcma` |
| `timeout` | 外呼超时时间 |
| `real_call_confirm` | 真实外呼确认开关 |

Bridge 发起的 FreeSWITCH originate 形态：

```text
originate {origination_caller_id_number=...,origination_caller_id_name=AI_Assistant,originate_timeout=...,ignore_early_media=true,call_id=...}sofia/gateway/{gateway}/{callee} ai-outbound-{callee} XML default
```

被叫接听后命中 `dialplan_outbound.xml` 中的 `ai-outbound` extension，执行：

```text
audio_stream ws://127.0.0.1:9003/audio/${uuid} 8000 L16
```

随后 Bridge 根据 FreeSWITCH 传来的 `${uuid}` 匹配预创建 session 中的 `fs_uuid`，再打开 TEN `/media` 会话。

## 6. 音频上行链路

FreeSWITCH 通过 `mod_audio_stream` 把电话音频推送到：

```text
ws://127.0.0.1:9003/audio/{call_id}
```

Bridge 的 `/audio/{call_id}` 支持两种帧：

- 二进制帧：直接视为音频 bytes。
- JSON 文本帧：支持 `{ "event": "media", "media": { "payload": "base64..." } }`。

Bridge 把音频封装成 TEN `/media` 协议：

```json
{
  "event": "media",
  "streamSid": "fs-{call_id}",
  "media": {
    "payload": "base64 audio"
  }
}
```

TEN `main_python` 收到音频后：

1. 根据 `start.media.encoding` 判断编码。
2. `linear16/pcm/raw/l16` 直接使用。
3. `pcma/alaw/g711a/a-law` 通过 `audioop.alaw2lin` 转 PCM。
4. 其他默认按 μ-law 通过 `audioop.ulaw2lin` 转 PCM。
5. 非 16k 音频通过 `audioop.ratecv` 重采样到 16k。
6. 构造 TEN `AudioFrame("pcm_frame")` 发给 ASR。

当前 FreeSWITCH dialplan 使用 `8000 L16`，因此实际主链路是：

```text
8k L16 -> Bridge base64 -> TEN /media -> main_python -> 16k PCM -> aliyun_asr
```

## 7. 开场白与对话链路

当 TEN `/media` 收到 start 事件后，`main_python` 执行 `on_websocket_connected`：

1. 发送 `start_connection` cmd 尝试唤醒 ASR。
2. 发送一段静音帧帮助 ASR 建立音频流。
3. 如果配置了 `greeting`，把开场白发给 TTS。
4. 同步把开场白作为 assistant transcript 发给 `message_collector`。

用户说话后：

1. ASR 返回 `ASRResultEvent`。
2. `main_python` 如果检测到用户插话，会先触发 `_interrupt()`：
   - flush LLM。
   - 给 TTS 发送 `tts_flush`。
   - 给 Bridge 发送 `ttsInterrupt`。
3. 如果 ASR 是 final：
   - 先走本地确定性回复，例如“今天几号”。
   - 否则把文本送入 LLM。
4. LLM 流式返回后，`main_python` 按句子切分，把完整句子逐段送给 TTS。
5. TTS 生成音频帧后回到 `main_python.on_audio_frame`。

## 8. TTS 下行与电话播放

TEN TTS 默认输出 16k PCM。

`main_python._send_audio_to_bridge` 会把 TTS 音频：

```text
16k PCM -> audioop.ratecv -> 8k PCM -> base64 -> WebSocket streamAudio
```

Bridge 收到 TEN 下行 `streamAudio` 后分两类处理：

- 如果没有 `fs_uuid`，说明是浏览器模拟链路，直接把音频推回浏览器 WebSocket。
- 如果有 `fs_uuid`，说明是真实 FreeSWITCH 通话，把 PCM 缓冲成短 WAV 文件，再通过 ESL `uuid_broadcast {fs_uuid} {wav_path} aleg` 播放给电话侧。

相关调优环境变量：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `TTS_MAX_PLAYBACK_CHUNK_MS` | `1200` | TTS 切片时长。越短越利于打断，过短可能卡顿 |
| `TTS_FLUSH_IDLE_MS` | `250` | TTS 空闲多久后把剩余 buffer 刷成 WAV |
| `TTS_PLAYBACK_GAIN` | `0.75` | 播放给 FreeSWITCH 前的音量增益 |
| `FS_BRIDGE_ALLOW_UUID_BREAK` | `false` | 是否允许插话时执行 `uuid_break` 中断 FreeSWITCH 当前播放 |

## 9. 真实外呼保护

为了避免开发阶段误拨真实号码，当前 Bridge 默认阻止真实外呼。

相关环境变量：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `REAL_CALL_ENABLED` | `false` | 是否允许真实外呼 |
| `DAILY_CALL_LIMIT_PER_NUMBER` | `3` | 每个号码每天最大外呼次数 |
| `ALLOWED_TEST_NUMBERS` | 空 | 如果设置，则只有白名单号码可拨 |
| `CALL_LIMIT_STORE` | `/tmp/fs_bridge_call_limits.json` | 外呼次数记录文件 |

要真正拨打运营商号码，必须同时满足：

1. `REAL_CALL_ENABLED=true`。
2. 请求体里 `real_call_confirm=true`。
3. 如果启用了 `ALLOWED_TEST_NUMBERS`，被叫号码必须在白名单。
4. 当天拨打次数不能超过 `DAILY_CALL_LIMIT_PER_NUMBER`。

这个保护是必要的。电话系统的第一性成本不是代码，而是真实号码、运营商限制、投诉风险和合规风险。

## 10. 录音 dump 与回放

Bridge 默认支持保存 `/audio/{call_id}` 收到的上行音频：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIO_DUMP_ENABLED` | `true` | 是否保存真实/模拟上行音频 |
| `AUDIO_DUMP_DIR` | `/tmp/fs_bridge_dumps` 或 `.env` 中的路径 | dump 目录 |

产物：

- `{call_id}.l16`：原始 8k L16 音频。
- `{call_id}.jsonl`：每帧时间偏移、字节数、来源等元数据。

调试接口：

| 接口 | 说明 |
|---|---|
| `GET /dumps` | 查看录音列表 |
| `WS /replay/{dump_name}` | 把历史录音重新喂给 TEN，听 TTS 回放效果 |

这个能力的价值是减少真实手机号测试次数。先用真实电话采一小批样本，再用 replay 反复调 ASR、LLM prompt、TTS 播放参数。

## 11. 调试与管理接口

Bridge 提供的主要接口：

| 接口 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 管理台页面 |
| `/health` | GET | Bridge 健康状态 |
| `/call-policy` | GET | 查询真实外呼策略与剩余额度 |
| `/originate` | POST | 发起 AI 外呼 |
| `/hangup` | POST | 主动挂断 |
| `/calls` | GET | 查看活跃通话列表 |
| `/calls/{call_id}` | GET | 查看单通电话详情 |
| `/audio/{call_id}` | WS | FreeSWITCH 音频入口 |
| `/simulate` | WS | 浏览器麦克风模拟 SIP 音频链路 |
| `/replay/{dump_name}` | WS | 回放 dump 到 TEN |
| `/chat` | POST | 不经过 SIP/TEN 的文本 LLM + TTS 调试 |
| `/chat/reset` | POST | 清理文本调试会话 |

TEN media 服务接口：

| 接口 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | TEN media 服务健康状态 |
| `/api/config` | GET | 当前 `/media` 地址配置 |
| `/media` | WS | Bridge 连接 TEN 的媒体入口 |

## 12. 启动方式

复制并配置环境变量：

```bash
cp .env.example .env
```

安装依赖：

```bash
task install
```

启动：

```bash
task run
```

健康检查：

```bash
curl http://127.0.0.1:9002/health
curl http://127.0.0.1:9003/health
```

## 13. 当前已知问题与生产化风险

### 13.1 命名遗留

`main_python` 里仍大量使用 `Asterisk` 命名，包括类名、配置名和日志。这是历史实现遗留，不影响当前 FreeSWITCH 链路，但会影响维护者理解。

建议后续统一改名：

- `AsteriskControlExtension` -> `SipControlExtension` 或 `FreeSwitchControlExtension`
- `AsteriskCallServer` -> `MediaCallServer`
- `asterisk_server_port` -> `media_server_port`

### 13.2 端口配置存在历史不一致

当前主要链路使用：

- Bridge：`9003`
- TEN `/media`：`9002`

但部分文件仍出现旧端口：

- `server/main.py` 注释和默认值里有 `9000/9001`。
- `server/fs_originate.py` 默认 `bridge_url` 是 `9001`。
- `freeswitch-config/dialplan_inbound.xml` 中 `audio_stream` 仍写 `9001`。

如果部署真实来电或使用旧脚本，必须先把端口统一到当前 Bridge 端口。

### 13.3 多路通话隔离不足

当前 `main_python` 中仍有固定 `session_id=54321`、固定 `stream_id` metadata 的痕迹。单路外呼测试可以工作，但生产多路并发会有状态串扰风险。

生产化必须做到：

- 每通电话独立 `call_id`。
- 每通电话独立 ASR metadata。
- 每通电话独立 LLM memory。
- TTS request 和打断事件按 `call_id` 精准路由。

### 13.4 动态 prompt/greeting/voice 尚未完全贯穿

`/originate` 请求模型已经支持 `prompt`、`greeting`、`voice`，Bridge 也保存到了 session，但当前 TEN `main_python` 主要仍使用 `property.json` 中的静态 `greeting` 和 LLM prompt。

如果业务系统需要每通电话不同话术，下一步要把这些动态字段从 Bridge start event 透传到 TEN `main_python`，并应用到 LLM/TTS。

### 13.5 FreeSWITCH 播放链路不是纯流式

真实电话 TTS 回放当前通过短 WAV + `uuid_broadcast` 实现，不是连续低延迟音频流。优点是实现简单、和 FreeSWITCH 兼容性好；缺点是插话打断粒度取决于 WAV 切片长度和 FreeSWITCH 播放控制。

如果后续要极致低延迟，需要评估更细粒度的 FreeSWITCH 媒体注入方式，而不是只调小 `TTS_MAX_PLAYBACK_CHUNK_MS`。

### 13.6 安全边界需要补齐

当前 Bridge 接口适合内网或开发环境。生产环境不能裸露以下接口：

- `/originate`
- `/hangup`
- `/audio/{call_id}`
- `/dumps`
- `/replay/{dump_name}`
- ESL `8021`

生产至少需要：

- 内网隔离或 API 网关。
- 鉴权签名。
- 请求审计。
- 号码白名单/黑名单。
- 调用频控。
- 密钥和真实号码不进入 Git。

## 14. 建议验收顺序

不要一开始就大量真实外呼。建议按下面顺序验收：

1. `curl /health` 确认 Bridge 和 TEN media 都启动。
2. 用 `/simulate` 管理台麦克风链路验证 ASR -> LLM -> TTS。
3. 用 `/chat` 验证 LLM key、TTS key、模型配置。
4. 用 FreeSWITCH 本机 `audio_stream` 模拟单通电话音频。
5. 打一通白名单真实号码，确认接通后能听到开场白。
6. 用户说一句短句，确认能识别并回复。
7. 用户在 TTS 播放时插话，观察是否触发打断。
8. 保存真实音频 dump，用 `/replay` 反复调参。
9. 再做多号码、多轮对话和长时间通话测试。
10. 最后才做并发、回调、失败重试和生产安全测试。

## 15. 后续优先级

按风险和收益排序：

1. 统一端口配置，修正旧 `9001/9000` 残留。
2. 删除或重命名 Asterisk 历史命名，降低维护误解。
3. 打通动态 `prompt/greeting/voice` 到 TEN 图内实际生效。
4. 做单通电话完整 e2e 测试脚本。
5. 做多路通话隔离，去掉固定 `54321`。
6. 给 `/originate`、`/hangup`、`/audio` 增加鉴权。
7. 优化 TTS 播放和打断策略。
8. 把真实 `.env`、`.DS_Store`、dump、临时 WAV、虚拟环境从版本控制中彻底排除。
