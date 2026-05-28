# 实时上下文真实链路验证记录

验证日期：2026-05-22

本文记录第一阶段上下文改造后的真实链路验证结果。这里的“真实链路”指：

```text
本地 MicroSIP 1000
  -> FreeSWITCH
  -> sip-realtime-voice-gateway
  -> 真实豆包 S2S Realtime
  -> sip-realtime-voice-gateway
  -> FreeSWITCH
  -> 本地 MicroSIP 1000
```

本次验证不等于真实运营商 SIP trunk 验证。运营商线路、真实手机号、公网 SIP/RTP、供应商 CDR 和号码规则仍属于后续单路真实线路验证范围。

## 1. 验证目标

本次只验证第一阶段上下文方案相关事实：

1. 本地电话媒体链路可以接入真实豆包 S2S。
2. 正常多轮问答可完成播放并进入 `committed_exchanges(status=completed)`。
3. 用户打断 assistant 播放时，网关不热重启豆包 session。
4. 用户打断 assistant 播放时，网关发送实时打断请求，并触发 FreeSWITCH break。
5. 被打断 assistant 回复进入 `committed_exchanges(status=interrupted)`。
6. 打断后不重放用户短音频缓存。
7. 新建豆包 session 时，`committed_exchanges` 可构造为 `dialog_context`，并被豆包用于回答前文问题。

## 2. 环境状态

验证前只读预检结论：

```text
.env：存在
gateway health：ok
FreeSWITCH container：healthy
MicroSIP 1000：已注册
realtime gateway：运行中
RTP relay：运行中
```

中途重启 gateway 时，`scripts/dev-local.sh restart` 重建了 FreeSWITCH 容器，导致 `1000` 注册短暂丢失。MicroSIP 重新注册后继续验证。

## 3. 正常链路验证

### 3.1 测试通话

```text
external_call_id：local-realtime-context-20260522-150405
call_id：efda9f0b589f40f19fa698d9422410fc
destination：1000
endpoint：sofia_contact:*/1000
```

通话结果：

```text
status：completed
hangup_cause：NORMAL_CLEARING
sip_status：200
media_connected_at_ms：有值
talk_duration_ms：18050
```

### 3.2 关键日志事实

```text
realtime_session_connected ... committed_history_turns=0 realtime_session_restarts=0
realtime_phone_server_vad_speech_started ... turn=1
realtime_phone_server_vad_turn_done ... turn=1 status=completed
gateway_conversation_turn_committed ... turn=1 committed_history_turns=1
freeswitch_realtime_session_finished ... interruptions=0 ... gateway_history_committed_turns=1 gateway_history_abandoned_turns=0
```

本通电话只验证了正常链路。因为用户只说了“你好”，豆包回复较短，没有足够插话窗口，所以没有触发打断。

验证结论：

```text
本地电话 -> FreeSWITCH -> 网关 -> 真实豆包 S2S -> 电话回放：通过
正常完成轮次进入 committed_exchanges：通过
```

## 4. 打断链路验证

### 4.1 测试通话

```text
external_call_id：local-realtime-interrupt-20260522-150602
call_id：bb94c65e4d9f4cd2981500660973bfc2
destination：1000
endpoint：sofia_contact:*/1000
```

通话结果：

```text
status：completed
hangup_cause：NORMAL_CLEARING
sip_status：200
media_connected_at_ms：有值
talk_duration_ms：56115
```

### 4.2 人工对话过程

本通电话中，用户先让 assistant 详细解释物业费：

```text
用户：请详细介绍一下物业费都包含哪些项目，慢慢说。
assistant：物业费呀，它一般会包含物业服务费，公共设施设备日常运行维护费用，还有绿化养护费、清洁卫生费、秩序维护费等等。
```

assistant 播放过程中，用户插话：

```text
用户：等一下，这个费用是什么？
```

### 4.3 关键日志事实

第一次有效打断：

```text
realtime_phone_server_vad_speech_started_used_for_interrupt ... provider_turn=3
gateway_conversation_turn_interrupted_committed ... turn=2 reason=server_vad_speech_started committed_history_turns=2 played_audio_ms=5495
realtime_playback_context_repair_started ... restart_on_interruption=False interrupted_text_chars=54
realtime_interruption_audio_discarded ... reason=server_vad_speech_started
freeswitch_playback_break_requested ... success=True
realtime_phone_playback_cleared ... interruptions=1 dropped_playback_frames=185 freeswitch_break_requests=1 realtime_interrupt_requests=1 realtime_interrupt_failures=0 context_repair_requests=1
```

后续又触发两次打断：

```text
gateway_conversation_turn_interrupted_committed ... turn=4 ... played_audio_ms=1829
gateway_conversation_turn_interrupted_committed ... turn=6 ... played_audio_ms=2263
```

最终 session 汇总：

```text
interruptions=3
dropped_playback_frames=293
dropped_stale_frames=0
freeswitch_break_requests=3
freeswitch_break_failures=0
realtime_interrupt_requests=3
realtime_interrupt_failures=0
context_repair_requests=3
realtime_session_restarts=0
gateway_history_committed_turns=8
gateway_history_abandoned_turns=0
replayed_input_frames=0
replayed_input_bytes=0
turns_completed=8
turns_failed=0
```

验证结论：

```text
真实电话链路内用户打断检测：通过
FreeSWITCH break：通过
豆包 realtime interrupt 请求：通过
打断后不热重启：通过
打断后不重放用户音频缓存：通过
被打断回复进入 committed_exchanges(status=interrupted)：通过
```

## 5. dialog_context 新 session 验证

### 5.1 验证方式

正常电话打断主路径不会创建新豆包 session，这是第一阶段设计目标。因此本项使用最小探针验证：

1. 用网关的 `committed_exchanges` 构造 `dialog_context`。
2. 新建一个真实豆包 S2S session。
3. 在 `StartSession` 中发送 `dialog_context`。
4. 询问豆包“根据刚才的对话，用户之前问物业费时，客服回答了哪些费用项目？”

### 5.2 发送的 dialog_context

```json
[
  {
    "role": "user",
    "text": "你好。"
  },
  {
    "role": "assistant",
    "text": "你好呀，今天过得怎么样？"
  },
  {
    "role": "user",
    "text": "请详细介绍一下物业费都包含哪些项目，慢慢说。"
  },
  {
    "role": "assistant",
    "text": "物业费呀，它一般会包含物业服务费，公共设施设备日常运行维护费用，还有绿化养护费、清洁卫生费、秩序维护费等等。"
  }
]
```

探针结果：

```text
probe_session_id：session_f0c1a953218b476da2e46bf747bf3928
dialog_context_items：4
first_audio_delta_ms：640
response_done_ms：2927
```

豆包返回：

```text
客服回答了物业费包含物业服务费、公共设施设备日常运行维护费、绿化养护费、清洁卫生费和秩序维护费。
```

验证结论：

```text
committed_exchanges -> dialog_context 序列化：通过
新豆包 session 接收 dialog_context：通过
豆包基于注入上下文回答前文问题：通过
```

## 6. 未覆盖项

本次验证未覆盖：

1. 真实运营商 SIP trunk 和真实手机号。
2. 公网 SIP/RTP、NAT、防火墙和供应商 CDR。
3. 多并发通话。
4. 进程崩溃后的跨进程恢复。
5. `ConversationRetrieve / ConversationCreate / ConversationTruncate`，第一阶段主路径本来不启用。
6. `dialog_id` 与 `dialog_context` 同时使用是否重复上下文。
7. 高噪声、弱网、长时间通话和大量打断场景。

## 7. 阶段结论

第一阶段商用上下文主路径在本地电话 + 真实豆包 S2S 条件下验证通过：

```text
正常对话：通过
用户打断：通过
打断入账：通过
不热重启：通过
不音频重放：通过
新 session dialog_context 恢复：通过
```

下一阶段建议先做 `committed_exchanges` 持久化和验证记录落库，再推进真实 SIP trunk 小样本验证。
