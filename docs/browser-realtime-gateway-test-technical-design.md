# 浏览器直连实时网关测试技术方案

日期：2026-05-27

## 1. 结论

浏览器直接接入当前实时语音网关是可行的，但它只能作为“模型对话效果验证”工具，不能替代软电话、SIPp、pjsua 或真实 SIP trunk 的电话链路验证。

推荐第一版新增一个浏览器测试页：

```text
GET /browser-realtime-test

Browser microphone
  -> Web Audio / AudioWorklet
  -> 8kHz s16le mono 20ms PCM frame
  -> ws://127.0.0.1:9101/media/browser-{timestamp}
  -> FreeSwitchRealtimeGatewayServer
  -> Doubao S2S realtime session
  -> 8kHz s16le mono playback frame
  -> Browser speaker
```

第一版不新增独立媒体协议，不改豆包 S2S 链路，不经过 FreeSWITCH，也不走 `POST /calls`。浏览器页面只复用现有媒体 WebSocket 的二进制 PCM 协议。

浏览器场景的目标不是纯手工 prompt 测试，而是业务链路仿真：开始对话前可从数据库加载 `strategy_core`、`speaking_style`、`opening_template`、债务信息和历史摘要，再临时编辑公共约束分块进行组合测试。编辑结果只绑定本次浏览器测试 `call_id`，不写入数据库，不修改代码内置公共约束，也不影响真实外呼、软电话、sip-provider 沙箱或生产 trunk。

## 2. 当前系统事实

### 2.1 已有真实电话链路验证

当前项目已经具备电话侧验证路径：

```text
软电话 / SIP endpoint
  -> FreeSWITCH
  -> 9199 dialplan
  -> mod_audio_stream
  -> ws://host.docker.internal:9101/media/fs/{uuid}
  -> Python realtime gateway
  -> Doubao S2S
  -> Python realtime gateway
  -> FreeSWITCH
  -> 电话侧播放
```

这个路径能验证 SIP、RTP、FreeSWITCH、9199、媒体 WebSocket、实时模型、AI 回放和打断控制。真实运营商线路仍需公网 SIP trunk 单通验证。

### 2.2 sip-provider 沙箱能力边界

`sip-provider-sandbox` 能模拟 trunk 形态和状态码，例如接通、183 后接通、408、486、503、508、603 等。它适合验证出局配置、号码格式、caller_id、SIP 状态映射和接通后进入 9199。

它不是浏览器语音端，也没有真人音频输入输出能力，因此不能验证“浏览器里和 AI 对话”的体验。

### 2.3 现有媒体 WebSocket 协议

实时网关当前已监听 FreeSWITCH 媒体入口：

```text
ws://{host}:9101/media/fs/{call_id}
ws://{host}:9101/media/{call_id}
```

入站音频要求：

```text
编码：PCM s16le
采样率：8000 Hz
声道：mono
帧长：20 ms
每帧样本：160 samples
每帧字节：320 bytes
传输方式：WebSocket binary frame
```

网关收到 320 字节帧后会重采样到 16k，送入豆包 S2S。模型输出音频会被网关重采样回 8k，再以 320 字节二进制帧发回 WebSocket 客户端。

控制消息目前只需要保留：

```json
{"type": "ping"}
```

返回：

```json
{"type": "pong", "call_id": "...", "session_id": "..."}
```

## 3. 浏览器直连方案

### 3.1 目标

第一版用于验证：

```text
1. 提示词和业务话术效果。
2. 真人和模型实时语音交互体感。
3. Server VAD 是否能识别浏览器麦克风输入。
4. AI 回放延迟和流畅度。
5. 普通插话打断体验。
6. 数据库策略、语气、开场白和公共约束组合后的整体效果。
7. 不依赖软电话时的快速产品演示。
```

第一版不验证：

```text
SIP 注册
SIP trunk
RTP 双向
PCMA 编码协商
FreeSWITCH dialplan
mod_audio_stream
电话侧 NAT
运营商状态码
真实号码外呼
```

### 3.2 页面入口

新增静态页面：

```text
static/browser-realtime-test.html
```

新增 HTTP 路由：

```text
GET /browser-realtime-test
```

导航里增加入口：

```text
浏览器对话
```

页面不自动打开麦克风，必须用户点击“连接并开始说话”后请求权限。

### 3.3 WebSocket 连接

页面生成测试 call_id：

```text
browser-{yyyyMMdd-HHmmss}-{random}
```

连接地址：

```text
ws://127.0.0.1:9101/media/{call_id}
```

如果页面不是运行在本机，可以根据 `location.hostname` 推导：

```text
ws://{location.hostname}:9101/media/{call_id}
```

第一版不使用 `/calls` 创建呼叫记录。这样可以避免把浏览器测试伪装成真实外呼。网关的 `media_connected` 回调找不到对应 call record 时会忽略，不影响媒体会话本身。

### 3.4 麦克风采集和编码

浏览器用 `AudioContext` 和 `AudioWorklet` 采集麦克风。输入通常是 48kHz 或 44.1kHz Float32 mono/stereo，需要转换为网关要求的 8kHz s16le mono。

处理流程：

```text
MediaStream
  -> AudioWorkletProcessor
  -> mixdown mono
  -> resample to 8000 Hz
  -> accumulate 160 samples
  -> Float32 [-1, 1] to int16 little-endian
  -> post 320 bytes to main thread
  -> WebSocket.send(ArrayBuffer)
```

关键约束：

```text
1. 每个 WebSocket binary message 必须是 320 bytes。
2. 不能把多个 20ms 帧合并成一个大包。
3. 不能发送 Float32Array、WAV、Opus、WebM 或 base64。
4. `ws.bufferedAmount` 过大时应丢弃新帧或暂停发送，避免积压造成延迟。
```

### 3.5 AI 音频播放

服务端返回的每个二进制消息也是 8kHz s16le mono PCM。浏览器不能直接播放 raw PCM，需要自定义播放器。

处理流程：

```text
WebSocket binary frame
  -> int16 little-endian to Float32
  -> append to playback ring buffer
  -> AudioWorkletProcessor
  -> resample 8000 Hz to AudioContext sampleRate
  -> speaker output
```

播放侧需要一个小缓冲，建议 120-240ms。缓冲太小会破音，缓冲太大会增加对话延迟。

### 3.6 页面状态

页面至少展示：

```text
连接状态：未连接 / 连接中 / 已连接 / 已断开 / 错误
call_id
WebSocket URL
麦克风权限状态
入站 RMS
出站播放 RMS
上行帧数
下行帧数
丢帧数
ws.bufferedAmount
最近日志
```

操作按钮：

```text
连接
断开
麦克风开关
扬声器静音
发送 ping
复制 call_id
```

### 3.7 浏览器业务仿真 Prompt Lab

浏览器页面增加业务仿真 Prompt Lab。用户在开始一轮浏览器对话前选择数据来源、加载数据库策略，再编辑本次测试覆盖项，点击“创建测试会话”后，后端生成一份只属于本次 `call_id` 的 `PromptSnapshot`。如果本次数据库数据包含开场白模板，后端同时生成 `PreparedOpeningAudio` 放入现有 `OpeningAudioStore`，浏览器媒体 WebSocket 建立后会像软电话接通一样先听到开场白。

重要约束：浏览器测试不得维护另一份公共约束默认值。默认规则必须来自当前真实业务拼接器和 `BUSINESS_*_RULES` 常量；页面传入的公共约束只作为本次 `browser-` 会话覆盖项追加到最终 prompt 中，便于测试效果，但不写回数据库、不修改生产常量。

支持两种数据来源：

```text
数据库组合模式：
  输入 callId / debtId / identityName / personaId
  -> 复用 PostgresPromptStore.prepare_business_prompt()
  -> 得到 strategy_core、speaking_style、opening_template、债务信息、历史摘要、PromptSnapshot、OpeningRequest
  -> 页面允许临时追加公共约束覆盖项和 speaking_style 覆盖项
  -> 注册 browser call_id 对应的测试 PromptSnapshot 和开场白音频

纯手工模式：
  不查数据库
  -> 使用页面填写的 employee_name、identityName、策略、金额等字段调用真实业务 prompt 拼接器
  -> 再追加页面公共约束覆盖项和 speaking_style 覆盖项
  -> 注册 browser call_id 对应的测试 PromptSnapshot
```

数据库组合模式下，浏览器页面至少支持这些输入：

```text
callId
debtId
identityName
personaId
是否生成并播放数据库开场白
是否允许页面 speaking_style 临时覆盖数据库 speaking_style
```

`personaId` 当前不参与数据库策略选择，实际策略阶段以 `debt_record.persona_id` 为准。页面传入值只做一致性校验：一致则记录为匹配，不一致则在响应 `warnings` 中提示“已使用数据库值”。

第一版支持这些本次会话覆盖项：

```text
employee_name
identityName
speaking_style
规则优先级
高优先级运行红线
对话风格
事实边界
身份核实与隐私边界
金额与争议处理
物业费场景约束
补充测试规则
```

会话创建接口：

```text
POST /browser-test-prompts
```

请求示例：

```json
{
  "call_id": "browser-20260527-143000-a1b2",
  "mode": "database",
  "context": {
    "callId": "990000000000032001",
    "debtId": "2056600544053252097",
    "identityName": "项目员工",
    "personaId": "3"
  },
  "opening": {
    "enabled": true
  },
  "overrides": {
    "speaking_style": "电话客服口吻，简短、自然。",
    "sections": {
      "critical_runtime": ["要求勿扰后必须礼貌结束。"],
      "extra": ["这是浏览器测试规则。"]
    }
  }
}
```

纯手工模式请求示例：

```json
{
  "call_id": "browser-20260527-143000-a1b2",
  "mode": "manual",
  "employee_name": "测试员工",
  "identityName": "项目员工",
  "speaking_style": "电话客服口吻，简短、自然。",
  "sections": {
    "rule_priority": ["数据库策略不得突破公共红线。"],
    "critical_runtime": ["要求勿扰后必须礼貌结束。"],
    "dialog_style": ["每次回复最多两句。"],
    "fact_boundary": ["不得编造系统未提供的信息。"],
    "privacy_disclosure": ["未确认身份前不得披露金额。"],
    "amount_dispute": ["金额争议以物业核实为准。"],
    "property_fee_scene": ["服务投诉先记录诉求。"],
    "extra": ["这是浏览器测试规则。"]
  }
}
```

响应示例：

```json
{
  "status": "ok",
  "call_id": "browser-20260527-143000-a1b2",
  "mode": "database",
  "prompt": {
    "scene": "browser-realtime-test",
    "version": "browser-test",
    "content_hash": "...",
    "preview": "最终拼接后的完整提示词..."
  },
  "sensitive_summary": {
    "amount_in_prompt": true,
    "amount_disclosure_requires_identity": true,
    "address_room_detail_excluded": true
  },
  "opening": {
    "status": "ready",
    "text_hash": "...",
    "voice": "..."
  },
  "warnings": [],
  "expires_in_seconds": 1800
}
```

后端保存方式：

```text
BrowserPromptTestStore
  call_id -> PromptSnapshot
  call_id -> context
  TTL: 30 分钟
  内存存储

OpeningAudioStore
  call_id -> PreparedOpeningAudio
  复用现有开场白播放入口
```

媒体连接时，`FreeSwitchRealtimeGatewayServer` 继续调用 `prompt_snapshot_provider(call_id)`。主进程提供一个组合 provider：

```text
1. 先查 BrowserPromptTestStore。
2. 查不到再查 OutboundCallManager.get_prompt_snapshot(call_id)。
3. 都查不到时走默认 prompt。
```

动态修改边界：

```text
1. 开始对话前编辑并创建测试会话，本次连接生效。
2. 对话中修改不会热更新当前豆包 session。
3. 对话中需要换规则时，必须断开并用新的 call_id 创建新测试会话。
4. 浏览器测试规则不落库，不进入真实外呼状态机。
5. 数据库策略只作为本次测试输入读取，不被浏览器页面回写。
```

## 4. 技术可行性评估

### 4.1 复用现有网关入口可行

现有网关已经支持 `/media/{call_id}` 路径，不强制要求 `/media/fs/{call_id}`。只要浏览器发送 320 字节二进制 PCM 帧，服务端会按电话媒体帧处理。

这意味着第一版主要是前端音频工程，不需要新增 Python 媒体服务。

### 4.2 最大风险是浏览器音频分帧

浏览器麦克风回调不是 20ms 一帧，常见 AudioWorklet quantum 是 128 samples。若直接发送 worklet 原始块，会导致服务端收到非 320 字节帧并丢弃。

解决方式是在浏览器端维护重采样累积缓冲，只在凑满 160 个 8k 样本时发送一帧。

### 4.3 第二风险是播放抖动

模型输出和网络调度可能导致下行帧不均匀。浏览器必须使用 ring buffer，而不是每收到一帧就临时创建 `AudioBufferSourceNode` 播放。后者容易出现卡顿、重叠和点击声。

### 4.4 第三风险是打断语义和电话侧不完全一致

浏览器直连会复用网关的 VAD 和播放队列逻辑，但绕过 FreeSWITCH 播放队列和 `uuid_audio_stream break`。因此它可以验证模型侧打断和本地播放清空体感，但不能证明 FreeSWITCH 侧旧音频一定被打断。

### 4.5 第四风险是测试规则污染生产规则

动态编辑公共约束只能存在于浏览器测试会话内。如果直接改代码常量、数据库策略或全局 prompt，会把测试规则带到真实外呼链路，风险不可接受。

解决方式是按 `call_id` 使用内存 `PromptSnapshot` 覆盖，且只允许 `browser-` 前缀的测试 `call_id` 注册测试 prompt。

## 5. 推荐实现阶段

### 阶段一：浏览器直连 MVP

改动范围：

```text
static/browser-realtime-test.html
app/health_server.py
app/browser_prompt_test.py
app/main.py
app/postgres.py
app/opening.py
tests/test_health_server.py
tests/test_browser_prompt_test.py
```

能力：

```text
1. 页面可打开。
2. 浏览器可连接 9101 `/media/{call_id}`。
3. 麦克风音频按 320 字节帧发送。
4. AI 返回音频可播放。
5. 页面展示帧数、RMS、连接状态和错误。
6. 页面可在开始对话前编辑公共约束分块，并为本次浏览器会话注册测试 prompt。
7. 页面可输入数据库上下文，组合数据库 `strategy_core`、`speaking_style`、`opening_template` 和页面公共约束。
8. 数据库组合模式可生成并播放本次浏览器会话开场白。
```

验收：

```text
1. `GET /browser-realtime-test` 返回 200。
2. `POST /browser-test-prompts` 能注册 browser call_id 的 PromptSnapshot。
3. 非 browser 前缀 call_id 不能注册测试 prompt。
4. 数据库组合模式能读取数据库策略并生成 browser-test PromptSnapshot。
5. 最终 prompt 预览能展示本次拼接结果和敏感字段摘要。
6. 数据库模式下金额进入 prompt 时，页面明确标记“身份确认后才可说”；地址、房号和费用明细不进入本轮 prompt。
7. 启用开场白时，浏览器连接后能先播放开场白。
8. `uv run --with pytest pytest tests/test_browser_prompt_test.py tests/test_health_server.py tests/test_main.py tests/test_realtime_phone_gateway.py tests/test_postgres.py tests/test_opening.py -q` 通过。
9. 手工浏览器测试能听到开场白和 AI 回复。
10. 网关日志出现 `prebuilt_prompt_snapshot_loaded version=browser-test`、`opening_playback_queued`、`freeswitch_realtime_media_connected` 和 `first_freeswitch_realtime_audio`。
11. 服务端没有持续 `frame_size_mismatch`。
```

### 阶段二：体验增强

可选能力：

```text
1. 展示数据库原始策略、页面覆盖后的最终 prompt diff。
2. 支持保存浏览器测试配置为本地 preset。
3. 展示首包延迟、首响延迟、回放缓冲深度。
4. 支持录制本次浏览器会话的本地音频片段。
5. 支持将 call_id 对应日志关键字一键复制。
```

注意：浏览器测试 `call_id` 不进入外呼状态机；开场白必须在 `/browser-test-prompts` 创建测试会话时显式生成并写入 `OpeningAudioStore`。

### 阶段三：自动化回归

浏览器手工测试不适合稳定 CI。自动化建议另走：

```text
Python WebSocket fake media client
  -> 发送固定 320 字节 PCM 帧
  -> 接收 AI 或 fake realtime session 输出
  -> 校验帧长、状态和统计
```

如果目标是电话链路自动化，则应做 SIPp 或 pjsua 被叫机器人，不应让浏览器承担电话链路回归。

## 6. 不推荐的替代方案

### 6.1 第一版不建议上 WebRTC

WebRTC 更接近浏览器实时音频，但会引入 ICE、DTLS、SRTP、SDP、证书和 FreeSWITCH Verto 配置。它适合后续做“浏览器当软电话”，不适合第一版快速验证模型对话效果。

### 6.2 第一版不建议新增 JSON/base64 音频协议

JSON/base64 会增加编码开销，也会让浏览器测试协议和电话媒体协议分叉。当前最好复用二进制 320 字节 PCM 帧，保证浏览器直连和 FreeSWITCH 媒体入口尽量一致。

### 6.3 第一版不建议把浏览器会话写入外呼记录

浏览器直连不是外呼，不应污染外呼状态机。需要保存测试结果时，应单独设计 `browser_test_session` 或日志导出，而不是伪造 `POST /calls`。

### 6.4 第一版不建议运行中热更新 prompt

豆包 S2S session 的角色和业务提示词在 `StartSession` 时确定。第一版不要在会话中途更新公共约束，避免出现浏览器显示已修改但模型仍按旧规则回复的错觉。需要验证新规则时，创建新的 browser call_id 并重连。

## 7. 安全和部署约束

浏览器麦克风要求安全上下文。`http://127.0.0.1` 和 `http://localhost` 属于浏览器允许的本地安全来源；如果部署到公网域名，需要 HTTPS。

本地测试可使用：

```text
http://127.0.0.1:9100/browser-realtime-test
ws://127.0.0.1:9101/media/{call_id}
```

当前页面在线上 HTTPS 来源下会默认生成同域媒体地址：

```text
https://test.example.com/browser-realtime-test
wss://test.example.com/media/{call_id}
```

推荐由反向代理把 `/media/` 转发到网关媒体端口，把页面、HTTP API 和媒体 WebSocket 都收敛到同一个 HTTPS 域名下：

```text
/browser-realtime-test              -> 127.0.0.1:19100
/browser-test-prompts/*             -> 127.0.0.1:19100
/ready                              -> 127.0.0.1:19100
/media/*                            -> 127.0.0.1:19101
```

如果线上是多实例，`POST /browser-test-prompts` 和后续 `/media/{browser_call_id}` 必须命中同一个 Python 实例，因为第一版的 `PromptSnapshot` 和开场白音频都在进程内存中。可选方案：

```text
1. 测试环境先单实例部署。
2. 反向代理按 browser_call_id 做 sticky session。
3. 后续把 BrowserPromptTestStore 和 OpeningAudioStore 改成 Redis 等共享存储。
```

公网或内网演示必须考虑：

```text
1. 不要把 9101 暴露给不可信网络。
2. WebSocket 需要鉴权或临时 token；测试环境至少要有登录保护、IP 白名单或 VPN。
3. 页面不要展示真实密钥。
4. 浏览器测试入口需要明确标识“非电话链路验证”。
5. HTTPS 页面不能连接明文 ws，需要改为 wss。
6. 浏览器测试页会展示最终 prompt、数据库策略和用户画像，不应作为公开生产入口。
```

## 8. 最终建议

建议实施阶段一，并把页面命名为“浏览器对话测试”，不要命名为“电话测试”。

第一版只解决一个问题：

```text
不打开软电话，也能快速和当前实时语音模型对话，验证话术、音色、延迟体感和普通打断体验。
```

它和现有电话链路测试是互补关系：

```text
浏览器直连：验证数据库策略、公共约束、开场白和模型对话体验，快。
本机软电话：验证本地电话媒体链路，真。
sip-provider 沙箱：验证 trunk 形态和状态码，稳。
真实 SIP trunk：验证生产线路，最终可信。
```
