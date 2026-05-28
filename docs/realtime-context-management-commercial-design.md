# 实时语音网关商用上下文管理技术方案

更新时间：2026-05-22

本文档用于给技术经理评审 SIP 实时语音网关在商用外呼场景下的多轮上下文、用户打断、热重启、跨进程和跨电话记忆方案。确认后可按本文档拆分实施计划。

参考文档：

- 火山引擎端到端实时语音大模型 API 接入文档，2.3 实时对话事件：<https://www.volcengine.com/docs/6561/1594356?lang=zh#_2-3-%E5%AE%9E%E6%97%B6%E5%AF%B9%E8%AF%9D%E4%BA%8B%E4%BB%B6>

## 1. 核心结论

当前商用方案采用“完整回复上下文模式”：

1. 正常通话中尽量保持同一个火山 S2S session，由火山服务端维护普通多轮上下文。
2. 网关本地维护 `committed_exchanges`，作为完整对话账本。
3. 用户打断 assistant 回复后，仍把本轮 assistant 完整回复写入 `committed_exchanges`。
4. 后续热重启或新 session 时，被打断的完整回复也可以当成用户已听完，作为完整 QA 注入 `dialog_context`。
5. 电话侧实际播放进度仍要记录，但只用于审计、质检、争议追溯和指标分析，不默认用于截断模型上下文。
6. 第一阶段主路径不加入 `ConversationRetrieve`、`ConversationCreate`、`ConversationTruncate`。
7. 第一阶段支持同进程内热重启恢复；不支持跨进程无缝恢复，跨进程只保留数据结构和持久化扩展点。

第一阶段必须做的事情：

- 本地停播和清队列。
- `ClientInterrupt` 打断服务端响应。
- 被打断回复完整入账到 `committed_exchanges(status=interrupted)`。
- 同进程内热重启或新 session 时，用 `dialog_context` 注入 `completed + interrupted` 完整 QA。
- 通话结果 payload 输出完整对话账本和播放事实。

第一阶段不建议做的事情：

- 不把 `ConversationRetrieve` 放进主链路。
- 不把 `ConversationCreate` 放进主链路。
- 不调用 `ConversationTruncate`。
- 不默认重放短缓存用户音频。

## 2. 第一性原理分析

### 2.1 模型上下文和播放事实是两件事

火山服务端上下文回答的是：模型下一句话应该参考哪些历史。

电话侧播放事实回答的是：用户实际听到了哪些音频。

这两者天然不相同。商用系统不能把它们混成一个字段，否则一旦用户打断，就会同时污染模型上下文和审计口径。

因此本方案分层：

- 模型上下文层：默认使用完整 QA。
- 播放事实层：记录 `playback_completed`、`played_audio_ms`、`heard_output_transcript`。

### 2.2 为什么打断后也保存完整回复

业务已经确认：后续恢复上下文时，被打断的 assistant 完整回复也可以当成用户已听完。

这意味着产品选择是：

```text
模型上下文认为：AI 已经完整回答过。
审计事实认为：电话侧没有完整播放完。
```

这不是技术天然事实，而是业务确认后的商用策略。技术上要把两个事实都记录下来，避免后续排查时说不清楚。

### 2.3 为什么主路径不需要 ConversationRetrieve / ConversationCreate

主路径同一个火山 session 内，服务端本来就知道自己生成过什么。我们不调用 `ConversationTruncate`，就没有主动把服务端上下文裁短。

因此：

- `ConversationRetrieve` 每次打断后查询服务端上下文，收益低，会增加链路复杂度、时延和失败分支。
- `ConversationCreate` 补写完整 QA 风险更大。如果服务端本来已经有这轮回复，再补写一次可能造成重复上下文。
- 商用主路径应优先依赖本地 `committed_exchanges` 做确定性恢复，而不是在打断时实时修改服务端上下文。

结论：第一阶段保留 `ConversationRetrieve / ConversationCreate` 为联调验证或异常诊断能力，不进入默认业务链路。

### 2.4 为什么不使用 ConversationTruncate

`ConversationTruncate` 的语义是按 `item_id` 和 `audio_end_ms` 把服务端上下文裁到用户实际听到的片段。这个语义适合“模型只能记住用户实际听到内容”的产品策略。

当前业务策略正好相反：被打断的完整回复也可按已听完恢复上下文。

因此第一阶段不启用 `ConversationTruncate`。如果未来业务改为“真实播放对齐模式”，再单独评估该能力。

## 3. 火山能力定位

### 3.1 dialog_id

`dialog_id` 是火山侧的服务端记忆索引。文档说明，服务端目前仅支持最近 20 轮 QA 对。

它不能替代网关本地账本，原因是：

- 它只代表火山服务端内部记忆。
- 它不记录电话侧是否完整播放。
- 它有最近 20 轮限制。
- 它无法覆盖跨进程无缝恢复、跨电话、审计和数据库持久化诉求。

### 3.2 dialog_context

`dialog_context` 是 `StartSession` 时传入的新 session 初始上下文。

本方案把它作为同进程内热重启和新 session 的主恢复工具；跨进程无缝恢复需要额外持久化支持，不属于第一阶段：

```json
"dialog_context": [
  {
    "role": "user",
    "text": "你是哪边？"
  },
  {
    "role": "assistant",
    "text": "我是物业中心小明，想和您核实一项物业费用事项。"
  },
  {
    "role": "user",
    "text": "这个费用是什么？"
  },
  {
    "role": "assistant",
    "text": "这是您三月份的物业费，账期是三月到四月，金额是12.34元。"
  }
]
```

构造规则：

1. 只使用 `committed_exchanges` 中的 `input_transcript` 和 `output_transcript`。
2. `completed` 和 `interrupted` 都可进入。
3. 保持原始时间顺序。
4. 必须成对传入 user / assistant，数组长度为偶数。
5. 超过上下文预算时，优先保留最近完整 QA，更早内容压缩成结构化摘要放入 `system_role`。

### 3.3 ClientInterrupt

`ClientInterrupt` 用于客户端打断服务端响应。

在本网关中，它的定位是：

- 本地先立即停播和清队列。
- 然后向火山发送 `ClientInterrupt`，尽量阻止服务端继续推当前响应。
- 无论 `ClientInterrupt` 是否成功，本地都要完成播放侧状态收敛，不能等远端 ack 才停播。

播放安全不能只依赖 `ClientInterrupt`。用户打断后，火山或网络链路仍可能有旧回复的迟到音频到达网关。网关必须按 `turn_id/reply_id` 做本地隔离：

1. 打断时把当前 assistant turn 标记为 `interrupted/closed`。
2. 停止当前电话侧播放任务。
3. 清空尚未播放的 AI 音频队列。
4. 后续如果继续收到旧 `turn_id/reply_id` 的音频帧，直接丢弃，不再进入播放队列。
5. 下一轮只播放新 `turn_id/reply_id` 的音频。

因此，`ClientInterrupt` 解决的是“通知火山别继续响应”；本地 `turn_id/reply_id` 过滤解决的是“旧音频不能再播给用户”。两者都需要。

### 3.4 ConversationRetrieve

第一阶段不进入主链路。

可选用途：

- 联调时验证 `ClientInterrupt` 后火山服务端上下文行为。
- 灰度抽样检查服务端是否保留完整 assistant 回复。
- 线上故障排查时对比服务端上下文和网关本地账本。

不建议用途：

- 每次打断都实时查询。
- 用它决定是否把本轮写入本地账本。
- 把它作为热重启前置必需步骤。

### 3.5 ConversationCreate

第一阶段不进入主链路。

原因：

- 正常同一个火山 session 内，服务端已经知道自己生成过的回复。
- 如果重复补写同一轮 QA，可能造成服务端上下文重复。
- 如果上下文已经不可控，直接热重启并用 `dialog_context` 重建更确定。

可选用途：

- 后续联调明确证明服务端确实会丢某些完整 QA。
- 且业务要求“不热重启也要原 session 补上下文”。
- 且能可靠避免重复补写。

### 3.6 ConversationTruncate

第一阶段不启用。

它只属于未来可选的“真实播放对齐模式”：如果业务改成模型只能记住用户实际听到内容，再启用 `dialog.extra.enable_conversation_truncate=true`，并按 `reply_id + played_audio_ms` 截断 assistant item。

## 4. 实施前代码现状

本节记录本方案实施前项目已有的基础历史状态，用于说明为什么需要本次改造：

- `ConversationExchange`：包含 `turn_id`、`input_transcript`、`output_transcript`。
- `pending_exchanges`：模型生成但尚未确认播放完成的临时历史。
- `committed_exchanges`：电话侧确认完整播放后提交的历史。

实施前行为：

1. 模型完成一轮后，用户输入和模型输出进入 `pending_exchanges`。
2. 电话侧播放完成后，pending 被提交到 `committed_exchanges`。
3. 用户打断时，当前 pending 被 abandoned，不进入 `committed_exchanges`。
4. 通话结束时，`committed_exchanges` 写入 call result payload。
5. 正常同一个火山 session 内，项目不会每轮把历史发给豆包，主要依赖火山服务端 session 自动上下文。
6. 热重启或新建 session 时，当前代码会把最近一部分 `committed_exchanges` 拼进 prompt 或 `dialog.system_role`，尚未使用火山原生 `dialog_context`。

实施前局限：

- 被打断回复没有进入完整对话账本。
- `committed_exchanges` 缺少播放状态字段。
- 未保存 `question_id`、`reply_id` 等火山侧 item 标识。
- 热重启恢复上下文依赖 prompt/system_role 拼接，不够结构化。
- `output_transcripts` 只表示模型生成过什么，缺少播放完成状态，不能单独作为商用审计账本。

## 5. 目标链路

### 5.1 正常多轮

```text
电话接通
  -> StartConnection
  -> StartSession
  -> 用户音频持续发送到同一个火山 session
  -> 火山服务端维护普通多轮上下文
  -> 模型返回 assistant 文本和音频
  -> 网关播放给电话侧
  -> 播放完成
  -> committed_exchanges 写入 status=completed
```

正常多轮不每轮重建 prompt，不每轮发送 `dialog_context`。

### 5.2 用户打断

```text
用户在 assistant 播放中说话
  -> 本地立即停止 AI 播放
  -> 清理尚未播放的 AI 音频队列
  -> 当前 assistant turn 标记为 interrupted/closed
  -> 记录 played_audio_ms / heard_output_transcript
  -> 发送 ClientInterrupt
  -> 丢弃后续到达的旧 turn_id/reply_id 音频
  -> committed_exchanges 写入 status=interrupted
  -> output_transcript 保存 assistant 完整回复
  -> 继续使用同一个火山 session
```

打断主路径不做：

- 不热重启。
- 不重放短缓存音频。
- 不调用 `ConversationRetrieve`。
- 不调用 `ConversationCreate`。
- 不调用 `ConversationTruncate`。

### 5.3 热重启或新 session

触发条件：

- 火山 WebSocket 断开。
- 火山 session 状态机异常。
- `ClientInterrupt` 失败后服务端仍持续推当前响应，且本地判断旧 session 不可继续安全使用。
- 进程重启、容器重启或其他导致内存 session 丢失的情况。

恢复行为：

```text
创建新火山 session
  -> 从本地 committed_exchanges 取最近完整 QA
  -> completed + interrupted 都进入 dialog_context
  -> system_role 只放业务规则和必要摘要
  -> 继续通话
```

注意：热重启恢复时不建议同时复用旧 `dialog_id` 又注入同一批 `dialog_context`，否则可能出现重复上下文。第一阶段建议优先采用“新 session + 本地 dialog_context”的确定性恢复方式。

## 6. 数据结构设计

说明：下面代码块中的 `@dataclass` 是 Python 的数据结构简写方式，用来声明一个主要承载字段的类。它会自动生成初始化等基础方法，方便把一轮对话、配置或状态作为一个结构化对象传递。它不是数据库表，也不是网络协议，只是代码里的数据对象定义。

### 6.1 RealtimeDialogConfig

建议扩展：

```python
@dataclass(frozen=True)
class RealtimeDialogContextItem:
    role: str
    text: str
    timestamp: int | None = None


@dataclass(frozen=True)
class RealtimeDialogConfig:
    bot_name: str | None = None
    system_role: str | None = None
    speaking_style: str | None = None
    model: str | None = None
    dialog_id: str | None = None
    dialog_context: tuple[RealtimeDialogContextItem, ...] = ()
```

构造 `StartSession` 时，把 `dialog_context` 放入 `dialog`：

```json
{
  "dialog": {
    "bot_name": "物业中心小明",
    "system_role": "...",
    "speaking_style": "...",
    "dialog_context": [
      {
        "role": "user",
        "text": "你是哪边？"
      },
      {
        "role": "assistant",
        "text": "我是物业中心小明，想和您核实一项物业费用事项。"
      }
    ],
    "extra": {
      "model": "1.2.1.1"
    }
  }
}
```

第一阶段不需要 `enable_conversation_truncate`。如果未来启用真实播放对齐模式，再扩展该字段。

### 6.2 ConversationExchange

建议将 `ConversationExchange` 升级为：

```python
@dataclass
class ConversationExchange:
    turn_id: int
    status: str
    input_transcript: str = ""
    output_transcript: str = ""
    heard_output_transcript: str = ""
    question_id: str | None = None
    reply_id: str | None = None
    played_audio_ms: int = 0
    playback_completed: bool = False
    source: str = ""
    created_at_ms: int | None = None
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `turn_id` | int | 是 | 网关本地轮次 ID |
| `status` | string | 是 | `completed` 或 `interrupted` |
| `input_transcript` | string | 是 | 用户本轮 ASR 文本 |
| `output_transcript` | string | 是 | assistant 完整回复；打断时也保存完整回复 |
| `heard_output_transcript` | string | 否 | 用户实际听到的 assistant 文本片段；未知时为空 |
| `question_id` | string/null | 否 | 火山 ASR 侧问题标识，能拿到就记录 |
| `reply_id` | string/null | 否 | 火山 assistant 回复标识，能拿到就记录 |
| `played_audio_ms` | int | 是 | 电话侧估算的实际播放毫秒数 |
| `playback_completed` | bool | 是 | 是否完整播放完成 |
| `source` | string | 是 | 本条记录进入 `committed_exchanges` 的来源，如 `playback_completed`、`client_interrupt`、`restart_rebuild` |
| `created_at_ms` | int/null | 否 | 写入本地账本的时间 |

状态语义：

- `completed`：assistant 回复完整播放完成。
- `interrupted`：assistant 回复被用户打断；恢复模型上下文时仍按完整 QA 使用。

`source` 和 `status` 的区别：

- `status` 表示本轮最终播放状态，例如完整播放或被打断。
- `source` 表示本条账本是由哪个流程写入的，例如播放完成回调、用户打断流程、热重启重建流程。

举例：`{"status": "interrupted", "source": "client_interrupt"}` 表示这轮因为用户打断流程进入账本，播放状态是未完整播放。

### 6.3 pending_exchanges

`pending_exchanges` 继续表示“模型已生成、但播放状态尚未最终确认”的临时轮次。

变化点：

- 播放完成时：pending 进入 `committed_exchanges(status=completed)`。
- 用户打断时：pending 不再直接丢弃，而是进入 `committed_exchanges(status=interrupted)`。
- 如果 pending 缺少完整 `output_transcript`，应尽量使用已收到的完整文本事件补齐；确实无法补齐时才标记为空并记录异常指标。

### 6.4 committed_exchanges

示例一：完整播放。

```json
{
  "turn_id": 1,
  "status": "completed",
  "question_id": "q_001",
  "reply_id": "r_001",
  "input_transcript": "你是哪边？",
  "output_transcript": "我是物业中心小明，想和您核实一项物业费用事项。",
  "heard_output_transcript": "我是物业中心小明，想和您核实一项物业费用事项。",
  "played_audio_ms": 2200,
  "playback_completed": true,
  "source": "playback_completed",
  "created_at_ms": 1770000000000
}
```

示例二：用户打断。

```json
{
  "turn_id": 3,
  "status": "interrupted",
  "question_id": "q_003",
  "reply_id": "r_003",
  "input_transcript": "这个费用是什么？",
  "output_transcript": "这是您三月份的物业费，账期是三月到四月，金额是12.34元。",
  "heard_output_transcript": "这是您三月份的物业费",
  "played_audio_ms": 820,
  "playback_completed": false,
  "source": "client_interrupt",
  "created_at_ms": 1770000005000
}
```

## 7. 播放进度和 heard_output_transcript

`played_audio_ms` 应基于电话侧真实播放估算：

```text
played_audio_ms = 已成功送到电话侧播放路径的有效帧数 * frame_duration_ms
```

性能要求：

- 播放进度记录必须是内存轻量计数，只更新当前 turn 的计数器和状态。
- 不允许在音频帧处理路径上做同步 IO、数据库写入、网络请求或大量日志输出。
- 不允许为了 `heard_output_transcript` 做实时复杂文本-音频强对齐。
- 指标和 payload 应在 turn 完成、用户打断、热重启或通话结束等状态边界写入。

按这个实现，`played_audio_ms` 和 `playback_completed` 的耗时应接近无感；`heard_output_transcript` 能可靠估算就写，不能可靠估算就留空。

不能直接使用：

- 模型生成音频总长度。
- 网关收到模型音频总长度。
- TTS 完成时间。

`heard_output_transcript` 可以有三种策略：

1. 完整播放时，等于 `output_transcript`。
2. 打断时，如果能根据句子边界或播放进度可靠估算，则记录估算片段。
3. 打断时，如果不能可靠估算，则留空，不猜测文本前缀。

“可靠估算”的定义是：网关能证明某段文本对应的音频已经实际进入电话侧播放路径，并且没有被停播或清队列撤销。

可以认为可靠的来源：

1. 模型或 TTS 事件提供句子级、片段级文本和音频边界。
2. 网关能建立音频 chunk 到文本片段的映射。
3. 电话侧播放队列确认对应音频帧已经播放，或至少已经进入不可撤销的播放路径。
4. 打断清队列发生在这些音频帧之后。

不能认为可靠的来源：

- 按文本长度比例猜。
- 按总音频时长比例切文本。
- 看到模型生成了文本，就认为用户听到了。
- TTS 完成了，就认为电话侧播完了。

注意：`heard_output_transcript` 不参与默认上下文恢复。恢复上下文使用 `output_transcript`。

## 8. call_result payload

通话结束时建议输出：

```json
{
  "call_id": "call_xxx",
  "session_id": "gateway_session_xxx",
  "status": "completed",
  "connected_at_ms": 1770000000000,
  "disconnected_at_ms": 1770000010000,
  "duration_ms": 10000,
  "prompt": {},
  "opening": {
    "text": "您好，系统显示您还有物业费未缴。",
    "text_hash": "opening_text_hash_xxx"
  },
  "turns": [
    {
      "role": "assistant",
      "text": "您好，系统显示您还有物业费未缴。"
    },
    {
      "role": "user",
      "text": "这个费用是什么？"
    },
    {
      "role": "assistant",
      "text": "这是您三月份的物业费，账期是三月到四月，金额是12.34元。"
    }
  ],
  "committed_exchanges": [
    {
      "turn_id": 1,
      "status": "interrupted",
      "question_id": "q_xxx",
      "reply_id": "r_xxx",
      "input_transcript": "这个费用是什么？",
      "output_transcript": "这是您三月份的物业费，账期是三月到四月，金额是12.34元。",
      "heard_output_transcript": "这是您三月份的物业费",
      "played_audio_ms": 820,
      "playback_completed": false,
      "source": "client_interrupt",
      "created_at_ms": 1770000005000
    }
  ],
  "metrics": {
    "interruptions": 1,
    "client_interrupt_requests": 1,
    "client_interrupt_failures": 0,
    "gateway_history_completed_turns": 0,
    "gateway_history_interrupted_turns": 1,
    "realtime_session_restarts": 0,
    "replayed_input_frames": 0,
    "replayed_input_bytes": 0
  }
}
```

字段口径：

- `committed_exchanges`：唯一权威完整对话账本，同时包含播放事实。
- `turns`：业务侧最终消费的对话消息流，第一条包含 assistant 开场白；它由 `opening.text` 和 `committed_exchanges` 派生。
- `input_transcripts/output_transcripts`：不再输出，避免形成第二套事实源；业务侧使用 `turns`，如需额外纯文本列表再从 `committed_exchanges` 派生。
- 恢复模型上下文默认使用 `committed_exchanges[*].output_transcript`。
- 审计和质检必须同时查看 `status/playback_completed/played_audio_ms/heard_output_transcript`。

## 9. 热重启、跨进程和跨电话

### 9.1 热重启

本文中的热重启不是重启网关进程，而是在同一通电话内结束旧火山 session、创建新火山 session。

第一阶段策略：

- 只支持同一网关进程内的热重启恢复。
- 能不热重启就不热重启。
- 必须热重启时，不依赖旧火山 session 内部状态。
- 从当前进程内存中的 `committed_exchanges` 取最近完整 QA。
- `completed` 和 `interrupted` 都作为完整 QA 放入 `dialog_context`。

第一阶段不承诺进程崩溃后的热重启恢复。进程一旦丢失，内存里的播放队列、pending turn 和未落库的 `committed_exchanges` 都可能丢失，这属于跨进程恢复范围。

### 9.2 跨进程

跨进程指网关进程、容器或实例重启后，内存中的 session 状态消失。

更具体地说，同一通电话还没结束，但处理它的运行进程变了，或原进程内存没了。例如：

- 网关进程崩溃后重启。
- 容器被 Kubernetes 重建。
- 服务发布导致实例重启。
- 负载转移后，通话被另一个网关实例接管。

跨进程的核心问题是：`pending_exchanges`、`committed_exchanges`、播放队列、当前 turn 状态默认都在内存里。进程没了，新进程不会天然知道这通电话之前说过什么、播到哪里、哪些回复被打断。

要支持跨进程恢复，必须把关键数据持久化：

- `call_id`
- `session_id`
- `committed_exchanges`
- 最近滚动摘要
- 当前通话状态
- 必要的业务主数据

第一阶段明确不做跨进程无缝恢复。进程丢失后，当前通话不保证恢复到原来的上下文和播放位置；系统只能重新建立通话或结束通话。即便如此，也要保证正常通话结束 payload 中包含足够完整的数据，后续可以落库并支持跨电话记忆。

### 9.3 跨电话

跨电话不应把上一通完整 transcript 全量塞给模型。

推荐带入：

1. 业务主数据：称呼、账单、费用状态、外呼策略。
2. 结构化通话摘要：身份确认、争议点、承诺事项、下一步动作。
3. 少量关键 QA 原话：来自 `committed_exchanges(status=completed|interrupted)` 的完整 QA。

下一通电话提示词必须明确：

```text
这是新的外呼。上一通电话内容仅作为业务背景，不代表用户本轮已经表达了这些内容。本轮仍需先确认身份，再根据已确认事实推进。
```

## 10. 超过 20 轮的策略

火山文档说明 `dialog_id` 服务端记忆目前仅支持最近 20 轮 QA。超过 20 轮后，不应假设服务端仍完整记得更早历史。

商用策略：

1. 网关全量保存 `committed_exchanges`。
2. 同一个火山 session 内继续依赖服务端最近上下文。
3. 网关生成结构化滚动摘要。
4. 热重启、新 session 或跨电话时，使用“滚动摘要 + 最近关键 QA”恢复。

摘要示例：

```text
# 已确认事实摘要
- 已确认对方为业主本人。
- 用户表示费用已缴，要求财务核对。
- 客服已说明会核对账务，不承诺减免。
- 用户对重复催缴不满，后续沟通应先说明核对进展。
```

摘要不能伪装成用户原话。

## 11. 短缓存音频策略

短缓存不是主路径，不用于解决上下文记忆。

可以保留最近 300 到 500ms 用户输入音频，但只在异常场景使用：

- 热重启或重连期间音频转发暂停。
- WebSocket 断开导致用户音频未送达。
- 状态机明确记录某段用户音频进入缓存但未发送给火山。

不触发补发的情况：

- 正常打断且音频持续发送给同一个火山 session。
- `ClientInterrupt` 成功或失败但本地已完成停播和入账。
- 缓存内容已经发给火山。
- 缓存疑似 AI 播放回声。
- VAD 判断没有有效用户语音。

## 12. 错误处理

### 12.1 ClientInterrupt 失败

处理：

1. 本地停播已经完成，不回滚。
2. `committed_exchanges(status=interrupted)` 正常写入完整 QA。
3. 标记 `client_interrupt_failures += 1`。
4. 如果火山仍继续推当前响应，丢弃该响应的后续音频。
5. 如果状态机不可控，热重启并用 `dialog_context` 恢复。

### 12.2 assistant 完整回复缺失

如果用户打断时，本地还没有拿到完整 `output_transcript`：

1. 优先等待当前 turn 已经到达的文本事件收敛。
2. 如果只收到部分文本，不要伪造完整回复。
3. 本轮仍可写 `committed_exchanges(status=interrupted)`，但 `output_transcript` 只能记录已确认文本，并打异常指标。
4. 该异常应作为联调重点，因为业务口径依赖“完整回复可恢复上下文”。

### 12.3 热重启失败

处理：

1. 保留本地 `committed_exchanges`。
2. 标记 `realtime_session_restarts` 和失败原因。
3. 如果无法恢复火山 session，应结束通话或进入人工兜底，不能继续播放不确定音频。

## 13. 指标和日志

第一阶段建议指标：

```text
interruptions
client_interrupt_requests
client_interrupt_failures
gateway_history_completed_turns
gateway_history_interrupted_turns
gateway_history_missing_output_turns
realtime_session_restarts
realtime_session_restart_failures
replayed_input_frames
replayed_input_bytes
```

可选联调指标，不进主路径：

```text
conversation_retrieve_requests
conversation_retrieve_failures
conversation_create_requests
conversation_create_failures
conversation_truncate_requests
conversation_truncate_failures
```

关键日志字段：

- `call_id`
- `session_id`
- `turn_id`
- `question_id`
- `reply_id`
- `status`
- `playback_completed`
- `played_audio_ms`
- `input_chars`
- `output_chars`
- `heard_output_chars`
- `client_interrupt_status`
- `dialog_id`

生产日志是否允许输出完整 transcript，要按隐私和合规要求确认。技术上建议 payload 可完整落库，普通日志只打长度、hash、turn_id 和状态。

## 14. 测试方案

### 14.1 单元测试

新增或调整测试：

1. `StartSession` payload 支持 `dialog_context`。
2. 默认 `StartSession` 不包含 `enable_conversation_truncate=true`。
3. `decode_event` 能解析 `question_id`、`reply_id`。
4. `ClientInterrupt` frame 构造正确。
5. 完整播放写 `committed_exchanges(status=completed)`。
6. 打断后写 `committed_exchanges(status=interrupted)`。
7. 打断后 `output_transcript` 保存完整 assistant 回复。
8. `heard_output_transcript` 未知时不猜测前缀。
9. 热重启时 completed 和 interrupted 都进入 `dialog_context`。
10. call result payload 输出完整对话账本和播放事实。

### 14.2 集成测试

使用 fake 火山 websocket 模拟：

1. 服务端返回用户 ASR、assistant 文本、assistant 音频。
2. 用户打断后，网关立即停播并清队列。
3. 网关发送 `ClientInterrupt`。
4. 网关把被打断 assistant 完整回复写入 `committed_exchanges(status=interrupted)`。
5. 后续同一 session 可继续处理用户语音。
6. 触发热重启时，新 `StartSession` 带上 `dialog_context`。

### 14.3 本地电话链路验证

使用 FreeSWITCH 9199 软电话验证：

1. 正常多轮上下文连续。
2. AI 回复中途插话，AI 立即停播。
3. 插话后不热重启。
4. 插话后模型可按完整回复上下文继续。
5. 通话结果中有 `completed` 和 `interrupted` 记录。
6. `played_audio_ms` 和 `playback_completed` 与电话侧表现基本一致。

### 14.4 生产前联调

必须与火山真实服务确认：

1. `ClientInterrupt` 是否支持当前 server_vad 电话链路。
2. 打断后服务端是否还会继续推当前响应音频。
3. assistant 完整文本事件在打断场景下是否稳定返回。
4. `dialog_context` 在新 `StartSession` 中的格式和上下文效果。
5. `dialog_id` 与 `dialog_context` 同时使用时是否会造成重复上下文。

可选确认：

1. `ConversationRetrieve` 返回格式，作为诊断能力。
2. `ConversationCreate` 是否可补写 QA，作为未来能力储备。
3. `ConversationTruncate` 是否支持当前模型，作为真实播放对齐模式储备。

## 15. 实施边界

### 15.1 第一阶段必做

- 升级 `ConversationExchange` 字段。
- 打断时不再 abandon pending，而是写入 `committed_exchanges(status=interrupted)`。
- `output_transcript` 保存 assistant 完整回复。
- 增加播放事实字段：`heard_output_transcript`、`played_audio_ms`、`playback_completed`。
- 增加 `ClientInterrupt` 发送能力。
- `StartSession` 支持 `dialog_context`。
- 同进程内热重启时，用内存中的 `committed_exchanges` 构造 `dialog_context`。
- call result payload 输出完整对话账本。
- 增加相关指标和测试。

### 15.2 第一阶段不做

- 不把 `ConversationRetrieve` 接入主链路。
- 不把 `ConversationCreate` 接入主链路。
- 不启用 `ConversationTruncate`。
- 不做跨进程无缝恢复。
- 不做跨电话长期记忆落库和摘要生成。
- 不做短缓存音频主路径重放。

### 15.3 第二阶段可做

- 持久化 `committed_exchanges`。
- 基于 Redis/DB 做跨进程恢复和实例接管。
- 生成通话结构化摘要。
- 下一通电话注入业务摘要和关键 QA。
- 灰度接入 `ConversationRetrieve` 做服务端上下文抽样诊断。
- 在明确需要时评估 `ConversationCreate` 或 `ConversationTruncate`。

## 16. 技术经理确认点

请重点确认：

1. 是否接受“完整回复上下文模式”：打断后完整 assistant 回复仍作为后续模型上下文。
2. `committed_exchanges` 是否按“完整对话账本”设计，而不是“用户实际听到文本账本”。
3. `status=interrupted` 是否允许进入 `dialog_context`。
4. 第一阶段是否确认不接入 `ConversationRetrieve / ConversationCreate / ConversationTruncate` 主链路。
5. 第一阶段是否确认只支持同进程内热重启恢复，不支持跨进程无缝恢复。
6. 热重启时是否采用“新 session + 本地 dialog_context”，避免旧 `dialog_id` 和 `dialog_context` 重复。
7. 生产环境 transcript 是完整落库、脱敏落库，还是只在 call result payload 中返回。
8. 如果打断时 assistant 完整文本尚未返回，业务是否接受记录已确认文本并打异常指标。

## 17. 最终原则

1. 火山服务端上下文负责同一个 session 内的自然多轮。
2. 网关本地 `committed_exchanges` 负责商用可审计完整对话账本。
3. 当前业务默认以 assistant 完整回复作为上下文事实，即使电话侧被打断。
4. 播放事实必须记录，但不默认影响模型上下文恢复。
5. 同进程内热重启和新 session 使用 `dialog_context` 做确定性恢复。
6. `dialog_id` 是火山服务端记忆索引，不能替代本地账本。
7. 短缓存音频只用于未送达用户语音兜底，不用于解决上下文记忆。
8. `ConversationRetrieve / ConversationCreate / ConversationTruncate` 都不是第一阶段主路径能力。
9. 跨进程无缝恢复需要持久化和实例接管能力，不属于第一阶段。
