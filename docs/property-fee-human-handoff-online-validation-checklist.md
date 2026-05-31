# 转人工线上最终验证清单

本文档只用于最后一轮线上验收。代码、Java 契约、失败状态机、人工 ASR 写入流程完成前，不提前执行完整线上联调。

## 1. 验证目标

最终验收只回答一个问题：

```text
客户在真实 AI 外呼中明确要求转人工后，
网页坐席能看到 AI 对话记录并点击接听，
FreeSWITCH 能把客户通道和坐席通道桥接，
整通电话结束后 AI + 人工 transcript 能统一写入 call_record 并回调 Java。
```

不在本轮验证范围内：

```text
监听质检
人工主动接管
回访
SIP 软电话正式兜底
人工录音长期保存
gateway_call_detail 独立表
```

## 2. 执行顺序

线上完整联调放到最后执行，但线上前置条件需要提前准备。

推荐顺序：

```text
1. 当前 PoC 代码整理并提交
2. 固定 Java / Python 接口契约和 transcript 数据格式
3. 补齐转人工失败状态机和日志
4. 接入人工阶段临时音频 + 后处理 ASR
5. 按本文档做线上最终验收
```

## 3. 环境前置清单

### 3.1 Web 页面与浏览器

| 检查项 | 期望结果 | 不通过时不要继续 |
|---|---|---|
| 坐席页面域名 | HTTPS 可访问 | 浏览器 WebRTC 权限可能不可用 |
| JsSIP 静态资源 | `/vendor/jssip.min.js` 返回 200 | 坐席不能注册 |
| 浏览器麦克风权限 | 坐席允许麦克风 | 能注册不代表能通话 |
| 浏览器兼容性 | Chrome / Edge 最新稳定版至少一种可用 | 先不要扩展到更多浏览器 |

### 3.2 FreeSWITCH WebRTC / WSS

| 检查项 | 期望结果 | 说明 |
|---|---|---|
| `mod_sofia` | 已加载 | SIP over WSS 依赖 |
| internal profile | `ws-binding` / `wss-binding` 可用 | 本地可用不等于公网可用 |
| WSS 域名 | 域名指向线上 FreeSWITCH 或反代入口 | 不使用裸自签证书域名 |
| WSS 证书 | 浏览器信任，域名匹配 SAN | `CN=FreeSWITCH` 自签证书不能商用 |
| Docker 端口 | WSS/WS 端口、RTP UDP 端口已映射 | RTP 端口缺失会接通无声 |
| 防火墙/安全组 | WSS、RTP、TURN 端口放通 | 同时查云安全组和系统防火墙 |
| Codec | 浏览器和 FreeSWITCH SDP 可协商 | 必要时确认 Opus / PCMU / PCMA 策略 |
| STUN/TURN | TURN 可用并配置到页面 | 外网复杂 NAT 下优先依赖 TURN |

### 3.3 Java 与数据库

| 检查项 | 期望结果 |
|---|---|
| Java 调用 `POST /calls` | 保持现有入参兼容 |
| `context.taskId` | flow callback 开启时必传 |
| `context.callId` | 对应 `public.call_record.id` |
| `call_record` 初始记录 | Java 已先创建 |
| Python 写 transcript | AI + 人工 turns 可写入同一个 `transcript.turns` |
| Java callback | 整通电话结束且 transcript 写完后只回调一次 |

### 3.4 人工阶段临时录音

| 检查项 | 期望结果 | 说明 |
|---|---|---|
| `RECORDING_ENABLED` | 线上最终验收时为 `true` | 普通 PoC 可关闭，完整 transcript 验收必须打开 |
| `RECORDING_DIR` | FreeSWITCH 进程可写，Python/ASR 后处理可读 | Docker 部署时要确认该目录是共享挂载 |
| `HUMAN_TRANSCRIPT_ENABLED` | 启用自动人工阶段后处理 ASR 时为 `true` | 未启用时由后处理服务手动调用 `/handoff/transcript` |
| `HUMAN_TRANSCRIPT_HTTP_URL` | 可从 Python 网关访问 | 接收录音路径和 call context，返回 `{"turns": [...]}` |
| 临时录音策略 | 只服务 ASR，不作为长期质检录音 | ASR 成功或复核完成后再按策略清理 |

## 4. 分阶段验证步骤

### 4.1 坐席注册

操作：

```text
1. 打开线上 `/webrtc-agent-test`
2. 填写线上 WSS 地址、坐席 SIP URI、测试密码
3. 点击上线注册
```

通过标准：

```text
页面显示 available
FreeSWITCH `sofia status profile internal reg` 能看到该坐席注册
浏览器控制台没有 WSS 证书或 SIP 注册错误
```

### 4.2 坐席 WebRTC 被叫

操作：

```text
1. 在页面点击“呼叫本座席”
2. 浏览器收到来电
3. 点击接听
```

通过标准：

```text
页面显示已接通
FreeSWITCH 能看到坐席 channel UUID
后端返回 agent_uuid
挂断后 `show channels` 无残留
```

失败重点：

```text
注册成功但无来电：查 WSS route、SIP contact、FreeSWITCH originate 返回。
来电后立刻失败：查 SDP、ICE candidate、codec、证书、NAT。
接通但无声：查 RTP 端口、TURN、浏览器麦克风、FreeSWITCH candidate。
```

### 4.3 AI 外呼到客户

操作：

```text
1. Java 或 `/outbound-test` 发起真实 AI 外呼
2. 客户接听
3. 确认 AI 正常听说
```

通过标准：

```text
`GET /calls/{call_id}` 进入 answered / media_connected
网关日志出现 `freeswitch_realtime_media_connected`
客户和 AI 双向音频正常
AI turns 正常产生
```

### 4.4 客户触发转人工

操作：

```text
客户明确说“我要转人工”或等价强表达。
```

通过标准：

```text
AI 不再继续催收
`GET /calls/{call_id}` 显示 handoff.state = waiting_agent
handoff.ai_turns 包含转人工前的 AI 对话记录
坐席页面待接通话列表出现该通电话
```

失败重点：

```text
未触发：查 ASR transcript、转人工关键词检测、实时网关 handoff 日志。
已触发但 AI 继续说话：查 playback break、realtime session close。
页面看不到：查 `/calls?status=active` 过滤和 handoff state。
```

### 4.5 坐席 claim 与 bridge

操作：

```text
1. 坐席点击待接通话
2. 确认页面能看到 AI 对话记录
3. 点击“接听选中通话”
4. 浏览器收到坐席来电并点击接听
```

通过标准：

```text
`POST /calls/{call_id}/handoff/claim` 返回 202
handoff.state 进入 human_active
handoff.agent_uuid 为后端生成并传给 FreeSWITCH 的坐席通道 UUID
handoff.bridge_reply 为 +OK
客户和人工坐席能双向通话
```

失败重点：

```text
claim 409：该通电话已被抢接、已过期或不是 waiting_agent。
originate 失败：查坐席 SIP 注册、WSS、contact、浏览器是否在线。
bridge 失败：查客户 call_id 是否仍是客户 FreeSWITCH channel UUID，查 agent_uuid 是否还在线。
originate 或 bridge 失败但总等待未超时：`GET /calls/{call_id}` 应回到 status = waiting_agent，保留 handoff.error，并允许其他坐席继续 claim。
bridge 失败后，已呼起的坐席 agent_uuid 通道应被挂断，避免坐席端残留空通话。
originate 或 bridge 失败且总等待已超时：`GET /calls/{call_id}` 应显示 handoff.state = handoff_failed，保留 handoff.error，并结束客户通话。
```

### 4.6 通话结束与 transcript

操作：

```text
1. 客户或坐席正常挂断
2. 人工阶段临时音频完成后处理 ASR
3. 自动 ASR 开启时由 Python 提交 HTTP JSON 转写任务
4. 自动 ASR 未开启时，由后处理服务调用 `/calls/{call_id}/handoff/transcript`
```

通过标准：

```text
call status = completed
handoff.state = completed
handoff.recording_status = completed
handoff.human_transcript_status = completed
call_record.transcript.turns 同时包含 AI turns 和人工 turns
人工 turns 可区分 human_agent 和 customer
Java 收到最终 callback
FreeSWITCH `show channels` 为 0 total
```

失败通过标准：

```text
人工录音或 ASR 失败时，handoff.human_transcript_status = failed
Python 只回调一次 Java FAILED
迟到的重复 transcript 结果不能覆盖已发送的 SUCCESS / FAILED 终态
```

## 5. 最终验收标准

必须全部满足：

```text
1. 真实客户外呼能接通并进入 AI 对话。
2. 客户明确要求转人工后，系统自动进入 waiting_agent。
3. 坐席页面能看到待接通话和 AI 对话记录。
4. 坐席 claim 后浏览器收到来电并接听。
5. FreeSWITCH bridge 后客户和人工能双向通话。
6. 通话结束后不提前 callback，必须等完整 transcript 写入后再 callback Java。
7. call_record.transcript.turns 包含 AI 阶段和人工阶段。
8. 异常结束后没有 FreeSWITCH channel 残留。
```

建议记录的验收证据：

```text
call_id
Java callId
agent_uuid
handoff state 时间线
uuid_bridge 返回
FreeSWITCH show channels 前后结果
call_record.transcript 样例
Java callback payload 样例
失败时的 FreeSWITCH cause / SIP status / 网关错误日志
```

## 6. 失败分流表

| 现象 | 优先检查 |
|---|---|
| 坐席页面打不开 | HTTPS、反代、静态资源 |
| JsSIP 注册失败 | WSS 证书、SIP URI、密码、FreeSWITCH profile |
| 注册成功但后端呼叫坐席失败 | `sofia_contact`、contact 可达性、浏览器是否保持在线 |
| 浏览器来电后立即失败 | SDP、ICE candidate、codec、mDNS、本地 IP 隐藏、TURN |
| 接通但无声 | RTP UDP 映射、防火墙、TURN、麦克风权限、`9196 echo` |
| 客户说转人工未触发 | ASR 文本、关键词检测、实时网关 handoff 日志 |
| 坐席 claim 失败 | handoff state、超时、重复抢接、客户是否已挂断 |
| bridge 失败 | 客户 channel UUID、agent_uuid、FreeSWITCH channel 状态 |
| transcript 未写入 | 人工 ASR HTTP 响应、`/handoff/transcript` 入参、Postgres 写入日志 |
| Java 未收到 callback | call_result writer、flow callback 配置、只回调一次的时序 |
| Java 收到重复 callback | 重复 `/handoff/transcript` 是否被幂等忽略、call_result writer 是否重复入队 |

## 7. 停止条件

出现以下情况时停止继续验证，先修根因：

```text
1. 9196 echo 或坐席自呼都不能稳定接通。
2. FreeSWITCH show channels 出现无法清理的残留通道。
3. 客户真实外呼基础链路不稳定。
4. WSS 证书不被浏览器信任。
5. TURN / RTP 问题导致外网坐席无声。
6. transcript 写入或 Java callback 出现重复、提前或丢失。
```

## 8. 回滚原则

线上验收失败时，优先保证普通 AI 外呼不受影响：

```text
1. 不改 Java 创建外呼入口。
2. 不改 FreeSWITCH 9199 AI 媒体主链路。
3. 可临时隐藏坐席 claim 入口或停用转人工触发。
4. 保留失败日志、call_id、agent_uuid 和 FreeSWITCH cause，再定位修复。
```
