# 物业费催收转人工技术方案

## 1. 背景和目标

当前项目用于物业费催收 AI 外呼。第一版转人工只解决一个明确场景：

```text
客户在 AI 通话中主动要求人工
-> 系统创建待接人工状态
-> 同一监控页面展示可接听
-> 坐席点击接听
-> 后端接入坐席浏览器 WebRTC 通道
-> FreeSWITCH 桥接客户线路和坐席线路
-> AI 停止说话
```

坐席接听主方案采用“网页版 WebRTC 坐席接入 FreeSWITCH”。SIP 软电话分机仅作为研发验证或异常兜底方案，不作为最终商用主路径。

本方案先不包含人工主动接管、监听质检、情绪触发接管、小区路由和独立坐席工作台。

## 2. 核心原则

1. 转人工由客户主动触发，例如“转人工”“找物业客服”“我不跟机器人说”。
2. 客户明确要求人工后，AI 不再继续催收，只做转接等待话术。
3. 转人工先进入待接状态，由坐席在页面点击接听；不直接向所有坐席发起语音接入。
4. 坐席接通后，AI 必须停止下行声音，避免 AI 和人工抢话。
5. 坐席未接通时，记录人工回访并礼貌结束，不回到 AI 继续催收。

## 3. 页面形态

第一版沿用现有“外呼监控与质检”页面，不单独做坐席工作台。

列表展示正在通话，并增加转人工状态：

```text
waiting_agent      客户要求人工，等待坐席接听
agent_claimed      坐席已点击接听，正在建立 WebRTC 坐席通道
human_active       人工已接入
callback_required  坐席未接通，需人工回访
```

页面操作：

```text
查看记录：只查看详情，不抢接
接听：抢接该通转人工电话
```

## 4. API 设计

### 4.1 正在通话列表

```http
GET /calls?status=active
```

用途：渲染监控列表，并显示每通电话的转人工状态。

示例字段：

```json
{
  "call_id": "call_123",
  "status": "media_connected",
  "duration_seconds": 135,
  "customer": {
    "name": "王芳",
    "phone": "138****8888",
    "community": "XX小区",
    "room": "1-2-301"
  },
  "emotion_state": "negative",
  "last_user_utterance": "我要转人工",
  "summary": "客户要求物业工作人员继续说明物业费情况。",
  "handoff": {
    "state": "waiting_agent",
    "trigger": "customer_requested",
    "reason": "request_human",
    "last_utterance": "我要转人工",
    "requested_at_ms": 1234567000,
    "expires_at_ms": 1234568000,
    "claimed_by": null,
    "can_claim": true
  }
}
```

### 4.2 通话详情

```http
GET /calls/{call_id}
```

用途：坐席查看客户信息、欠费信息、完整对话和转人工上下文。

建议字段：

```json
{
  "call_id": "call_123",
  "external_call_id": "java_call_001",
  "task_id": "task_001",
  "debt_id": "debt_001",
  "status": "media_connected",
  "duration_seconds": 135,
  "customer": {
    "name": "王芳",
    "phone": "138****8888",
    "community": "XX小区",
    "room": "1-2-301"
  },
  "debt_summary": {
    "status": "存在待处理物业费",
    "period": "2024年下半年至2025年上半年",
    "amount": "1280.00",
    "currency": "CNY"
  },
  "handoff": {
    "state": "waiting_agent",
    "trigger": "customer_requested",
    "reason": "request_human",
    "last_utterance": "我要转人工",
    "requested_at_ms": 1234567000,
    "expires_at_ms": 1234568000,
    "claimed_by": null,
    "can_claim": true
  },
  "turns": [
    {"role": "assistant", "text": "请问您方便核实一下..."},
    {"role": "user", "text": "我要转人工"}
  ],
  "summary": "客户要求转人工，希望物业工作人员继续说明物业费情况。"
}
```

说明：

```text
列表接口返回轻量摘要，详情接口返回完整上下文。
summary 是体验增强字段，可为空，不应阻塞转人工链路。
详情页可展示金额，但 AI 通话中不主动向客户暴露金额。
```

### 4.3 创建转人工请求

```http
POST /calls/{call_id}/handoff
```

调用时机：AI/网关识别到客户明确要求人工时自动调用。

请求示例：

```json
{
  "trigger": "customer_requested",
  "reason": "request_human",
  "last_utterance": "我要转人工",
  "queue_id": "property-fee-default"
}
```

接口职责：

```text
1. 校验通话仍在进行中。
2. 校验当前没有正在进行的 handoff。
3. 设置 handoff.state = waiting_agent。
4. 记录客户原话、触发原因、开始时间和过期时间。
5. 触发 AI 等待话术：好的，我帮您联系物业工作人员，请稍等。
6. 让 GET /calls?status=active 返回可接听状态。
```

该接口不负责桥接电话，只负责创建“待人工接听”状态。

### 4.4 坐席抢接

```http
POST /calls/{call_id}/handoff/claim
```

调用时机：坐席在页面点击“接听”。

请求示例：

```json
{
  "agent_id": "agent_1001",
  "agent_name": "张三",
  "agent_session_id": "web_agent_session_1001"
}
```

接口职责：

```text
1. 原子抢占该通 handoff，防止多个坐席同时接同一通电话。
2. 设置 handoff.state = agent_claimed。
3. 后端为该坐席建立或复用浏览器 WebRTC 通道。
4. 坐席 WebRTC 通道接通后桥接客户线路和坐席线路。
5. 桥接成功后设置 handoff.state = human_active。
```

如果已被其他坐席抢接，返回冲突：

```json
{
  "status": "conflict",
  "error": "handoff already claimed"
}
```

## 5. 状态流转

```text
AI 正常通话
  -> 客户要求人工
  -> POST /calls/{call_id}/handoff
  -> waiting_agent
  -> 坐席点击接听
  -> POST /calls/{call_id}/handoff/claim
  -> agent_claimed
  -> 坐席 WebRTC 通道接通
  -> human_active
```

失败路径：

```text
waiting_agent 超时无人接
  -> callback_required
  -> AI 播放未接通话术
  -> 记录人工回访
  -> 礼貌结束通话
```

```text
agent_claimed 后坐席 WebRTC 通道未建立
  -> 释放坐席锁
  -> 若总等待未超时，回到 waiting_agent
  -> 若总等待已超时，进入 callback_required
```

建议超时：

```text
总转人工等待时间：15 秒
坐席 WebRTC 通道建立等待：8-10 秒
```

## 6. WebRTC 坐席接入方案

### 6.1 推荐路径

最终商用建议让浏览器坐席通过 WebRTC 接入 FreeSWITCH，而不是单独开发一套网页音频通道。

推荐链路：

```text
坐席浏览器
  -> WebRTC / WSS
  -> FreeSWITCH
  -> uuid_bridge
  -> 客户当前 call_id
```

这样坐席浏览器在 FreeSWITCH 里仍然是一条可管理的通话通道，可以复用 FreeSWITCH 的桥接、挂断、事件、录音和质检能力。

不建议第一版自研：

```text
坐席浏览器
  -> 自研 WebSocket 音频
  -> Python 网关混音/转发
  -> 再送回 FreeSWITCH
```

原因是自研音频通道需要额外处理实时双向音频、回声消除、抖动缓冲、断线重连、录音质检、挂断状态和 FreeSWITCH channel 对齐，容易把项目拖成自研媒体服务器。

### 6.2 接入方式候选

FreeSWITCH 侧可以由技术经理评估两条标准路径：

```text
1. SIP over WebSocket / WSS
   浏览器使用 JsSIP / SIP.js 注册为 WebRTC 分机，FreeSWITCH 仍按 user/1001 这类 endpoint 管理。

2. FreeSWITCH Verto
   浏览器通过 mod_verto 接入 FreeSWITCH，适合直接做 WebRTC 客户端能力。
```

第一版不在本文档中强行指定二选一，但目标是：坐席网页点击接听后，后端能拿到一个可桥接的 `agent_uuid`，再与客户 `call_id` 执行桥接。

### 6.3 SIP 软电话兜底

如果 WebRTC PoC 短期不稳定，可以临时使用 SIP 软电话分机兜底：

```text
网页仍负责查看记录和点击接听
真实语音临时由 Linphone / Zoiper / 企业 SIP 分机承载
后端 bridge 客户 call_id 和 SIP 坐席通道
```

该兜底方案只用于验证和应急，不作为最终商用体验。

## 7. FreeSWITCH 接线设计

当前项目外呼时使用：

```text
originate {origination_uuid=<call_id>, sip_realtime_gateway_call_id=<call_id>}<endpoint> 9199 XML default
```

9199 接通后启动 `uuid_audio_stream`，把客户通话音频接入实时媒体网关，并在 FreeSWITCH 中 `park` 保持通道在线。

坐席抢接成功后，主路径应建立浏览器 WebRTC 坐席通道，并拿到坐席通道的 `agent_uuid`。

如果使用 SIP over WebSocket 注册模式，坐席浏览器可表现为 FreeSWITCH 内部 endpoint，例如：

```text
originate {origination_uuid=<agent_uuid>,originate_timeout=10}user/1001 &park()
```

如果使用 Verto 或其它 WebRTC 接入方式，则由对应模块生成或接入坐席通道；核心要求仍然是得到可 bridge 的 `agent_uuid`。

坐席 WebRTC 通道接通后：

```text
uuid_audio_stream <call_id> break
uuid_bridge <call_id> <agent_uuid>
```

含义：

```text
WebRTC 坐席通道：坐席浏览器在 FreeSWITCH 中对应的一路通话
park：坐席接通后先停在 FreeSWITCH 等待桥接
uuid_audio_stream break：清掉客户通道上当前 AI 播放队列
uuid_bridge：把客户线路和坐席线路接到一起
```

重要边界：

```text
uuid_audio_stream break 只清理当前播放，不等于永久关闭 AI。
进入 human_active 后，实时媒体网关必须禁止该 call_id 的 AI 下行音频。
```

## 8. AI 和意图识别

第一版只自动触发强规则：

```text
转人工
找人工
找真人
找物业客服
让工作人员跟我说
我不跟机器人说
不要机器人
```

疑似表达只提示，不自动转：

```text
你是谁
你是机器人吗
别跟我绕
叫负责人来
让你们物业处理
```

识别到强规则后，AI 只说一句等待话术：

```text
好的，我帮您联系物业工作人员，请稍等。
```

之后 AI 不再继续催收。

## 9. 回访记录

进入 `callback_required` 时，应记录：

```text
call_id
external_call_id
task_id
debt_id
客户信息
客户原话
转人工原因
turns
summary
失败原因：无人接 / 坐席 WebRTC 通道未建立 / 坐席忙线 / 坐席页面断开
```

第一版可以先在网关内存状态和既有回调/落库链路中体现；正式商用建议落库为人工回访任务。

## 10. 安全和合规边界

1. 详情页可展示欠费金额给坐席，但 AI 通话中不主动向客户暴露具体金额。
2. 客户明确要求勿扰、拒绝继续沟通时，不应强行转人工继续催收，应记录并礼貌结束。
3. 坐席接通后 AI 必须退出说话，避免误导客户或形成多人抢话。
4. 转人工状态、坐席抢接、桥接成功/失败需要记录审计日志。

## 11. 实施阶段

### MVP

```text
1. 新增 handoff 状态字段。
2. 新增 POST /calls/{call_id}/handoff。
3. 新增 POST /calls/{call_id}/handoff/claim。
4. GET /calls 和 GET /calls/{call_id} 返回 handoff 信息。
5. 完成浏览器 WebRTC 坐席接入 FreeSWITCH 的 PoC。
6. 坐席 claim 后建立或复用 WebRTC 坐席通道，并获取 agent_uuid。
7. FreeSWITCH ESL 实现 park + uuid_bridge。
8. 网关进入 human_active 后停止 AI 下行。
```

### 后续增强

```text
1. 正式坐席在线/忙闲状态。
2. 多坐席排队和分配策略。
3. 独立坐席工作台。
4. 人工主动接管。
5. 监听质检。
6. 回访任务落库和统计报表。
7. SIP 软电话兜底切换和异常恢复策略。
```

## 12. 需要技术经理确认的问题

1. 浏览器 WebRTC 接入 FreeSWITCH 选择 SIP over WebSocket / WSS，还是 mod_verto。
2. 线上是否具备 HTTPS / WSS / STUN / TURN 条件；坐席跨网络办公时 TURN 是否必须。
3. 坐席点击接听前是否要求浏览器预先完成麦克风授权和 WebRTC 注册。
4. 坐席未接后的回访任务落库归属：网关直接写库，还是通过 Java 业务系统回调生成。
5. 当前 `call_id` 是否稳定等于可 bridge 的 FreeSWITCH channel UUID；若真实线路中存在 B-leg 差异，需要确认实际 bridge 目标 UUID。
6. 人工接通后是否需要继续保留媒体 WebSocket 只做记录，还是关闭该 call 的 AI 会话。
7. SIP 软电话兜底是否保留，以及兜底时的坐席 endpoint 命名和权限策略。
