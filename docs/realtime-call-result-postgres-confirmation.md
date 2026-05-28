# 通话结果 PostgreSQL 持久化确认文档

确认日期：2026-05-22

本文用于给技术经理确认：实时语音网关在每通电话结束后，应该如何把通话结果写入 PostgreSQL。

本方案的核心原则：

1. `committed_exchanges` 是唯一权威对话账本。
2. `turns` 是业务侧最终消费的对话消息流，第一条需要包含 assistant 开场白。
3. 不再保存 `input_transcripts` / `output_transcripts` 两个派生数组。
4. PostgreSQL 第一版只建一张主表，常用查询字段单独成列，完整通话事实放入 `payload jsonb`。
5. `metrics` 只保留商用第一版必要字段，过细的音频帧、队列、RMS 调试指标先只留日志，不进入数据库 payload。

## 1. 当前状态

当前代码在通话结束后会执行以下动作：

1. 关闭豆包 realtime session。
2. 结束播放任务。
3. 从内存活跃 session 中移除当前 session。
4. 打印 `freeswitch_realtime_session_finished` 汇总日志。
5. 如果配置了 `call_result_writer`，会生成 call result payload 并入队。

但当前 `PostgresRuntime.call_result_writer` 还是 `None`，所以目前不会把 `committed_exchanges` 写入数据库。

下一步要做的是实现 PostgreSQL `call_result_writer`，在通话结束时把 call result payload 写入数据库。

## 2. PostgreSQL 表设计

建议第一版只建一张表：

```sql
create table realtime_call_results (
  id bigserial primary key,
  call_id text not null unique,
  session_id text not null,
  status text not null,
  connected_at_ms bigint not null,
  disconnected_at_ms bigint not null,
  duration_ms integer not null,
  committed_turns integer not null default 0,
  completed_turns integer not null default 0,
  interrupted_turns integer not null default 0,
  payload jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

建议索引：

```sql
create unique index realtime_call_results_call_id_idx
  on realtime_call_results (call_id);

create index realtime_call_results_created_at_idx
  on realtime_call_results (created_at desc);

create index realtime_call_results_status_idx
  on realtime_call_results (status);
```

如果后续需要按打断数量检索，也可以补充：

```sql
create index realtime_call_results_interrupted_turns_idx
  on realtime_call_results (interrupted_turns);
```

## 3. 表字段说明

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `id` | `bigserial` | 是 | 数据库自增主键，只用于数据库内部定位。业务侧不要依赖这个字段做幂等。 |
| `call_id` | `text` | 是 | 本通电话唯一 ID。用于防止同一通电话重复插入。建议作为 upsert 的唯一键。 |
| `session_id` | `text` | 是 | 网关实时媒体 session ID。一通电话媒体连接对应一个 gateway session。 |
| `status` | `text` | 是 | 通话结果状态。第一版通常为 `completed`，后续可扩展 `failed`、`canceled` 等。 |
| `connected_at_ms` | `bigint` | 是 | 电话媒体 WebSocket 接入网关的时间，毫秒时间戳。 |
| `disconnected_at_ms` | `bigint` | 是 | 电话媒体 WebSocket 断开的时间，毫秒时间戳。 |
| `duration_ms` | `integer` | 是 | 媒体连接持续时长，通常等于 `disconnected_at_ms - connected_at_ms`。 |
| `committed_turns` | `integer` | 是 | 已进入 `committed_exchanges` 的对话轮数。包含完整播放轮次和打断入账轮次。 |
| `completed_turns` | `integer` | 是 | 完整播放完成并入账的轮数，即 `committed_exchanges[*].status = completed` 的数量。 |
| `interrupted_turns` | `integer` | 是 | 被用户打断但仍入账的轮数，即 `committed_exchanges[*].status = interrupted` 的数量。 |
| `payload` | `jsonb` | 是 | 完整通话结果 JSON。包含 prompt、opening、turns、committed_exchanges、metrics 等完整事实。 |
| `created_at` | `timestamptz` | 是 | 这条数据库记录首次写入时间。 |
| `updated_at` | `timestamptz` | 是 | 这条数据库记录最后更新时间。重复写入同一个 `call_id` 时更新。 |

## 4. 为什么采用“主表 + JSONB”

第一版不建议把 `committed_exchanges` 拆成多张明细表，原因：

1. 当前优先目标是可靠保存完整通话事实。
2. `committed_exchanges` 字段还可能随商用验证继续微调。
3. JSONB 可以完整保存结构，后续字段扩展不需要频繁改表。
4. 常用查询字段已经单独成列，不影响基本检索。

未来如果要做质检报表、按每轮 QA 查询、按 interrupted turn 聚合统计，再增加 `realtime_call_exchanges` 明细表。

## 5. payload JSON 结构

第一版建议写入 `payload` 的 JSON：

```json
{
  "call_id": "call_xxx",
  "session_id": "gateway_session_xxx",
  "status": "completed",
  "connected_at_ms": 1779433563932,
  "disconnected_at_ms": 1779433620033,
  "duration_ms": 56101,
  "prompt": {
    "scene": "default",
    "version": "inline",
    "content_hash": "hash_xxx",
    "instructions": "本通电话使用的业务提示词",
    "loaded_at_ms": 1779433563000,
    "metadata": {
      "source": "postgres",
      "identityName": "项目员工",
      "personaId": "7",
      "debtId": "2056563388954320898",
      "employee_name": "物业中心小明"
    }
  },
  "opening": {
    "text": "您好，系统显示您还有物业费未缴。",
    "text_hash": "opening_text_hash_xxx",
    "voice": "female_warm",
    "speaker": "zh_female_vv_jupiter_bigtts",
    "playback_frames": 120,
    "playback_interrupted": false
  },
  "turns": [
    {
      "role": "assistant",
      "text": "您好，系统显示您还有物业费未缴。"
    },
    {
      "role": "user",
      "text": "你好。"
    },
    {
      "role": "assistant",
      "text": "你好呀，今天过得怎么样？"
    }
  ],
  "committed_exchanges": [
    {
      "turn_id": 1,
      "status": "completed",
      "question_id": null,
      "reply_id": null,
      "input_transcript": "你好。",
      "output_transcript": "你好呀，今天过得怎么样？",
      "heard_output_transcript": "你好呀，今天过得怎么样？",
      "played_audio_ms": 2808,
      "playback_completed": true,
      "source": "playback_completed",
      "created_at_ms": 1779433570447
    },
    {
      "turn_id": 2,
      "status": "interrupted",
      "question_id": null,
      "reply_id": null,
      "input_transcript": "请详细介绍一下物业费都包含哪些项目，慢慢说。",
      "output_transcript": "物业费一般包含物业服务费、公共设施设备维护费、绿化养护费、清洁卫生费、秩序维护费等等。",
      "heard_output_transcript": "",
      "played_audio_ms": 5495,
      "playback_completed": false,
      "source": "client_interrupt",
      "created_at_ms": 1779433582889
    }
  ],
  "metrics": {
    "interruptions": 1,
    "freeswitch_break_requests": 1,
    "freeswitch_break_failures": 0,
    "realtime_interrupt_requests": 1,
    "realtime_interrupt_failures": 0,
    "realtime_session_restarts": 0,
    "gateway_history_committed_turns": 2,
    "gateway_history_completed_turns": 1,
    "gateway_history_interrupted_turns": 1,
    "gateway_history_abandoned_turns": 0,
    "gateway_history_missing_output_turns": 0,
    "replayed_input_frames": 0,
    "turns_started": 2,
    "turns_completed": 2,
    "turns_failed": 0
  }
}
```

## 6. payload 顶层字段说明

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `call_id` | `string` | 是 | 本通电话唯一 ID。与表字段 `call_id` 一致。 |
| `session_id` | `string` | 是 | 网关实时媒体 session ID。与表字段 `session_id` 一致。 |
| `status` | `string` | 是 | 通话结果状态。第一版通常为 `completed`。 |
| `connected_at_ms` | `number` | 是 | 电话媒体接入网关的时间，毫秒时间戳。 |
| `disconnected_at_ms` | `number` | 是 | 电话媒体断开时间，毫秒时间戳。 |
| `duration_ms` | `number` | 是 | 媒体连接持续时长。 |
| `prompt` | `object` | 是 | 本通电话使用的提示词快照。用于问题复盘和模型行为追溯。 |
| `opening` | `object` | 是 | 本通电话开场白信息。没有开场白时字段值可为空或默认值。 |
| `turns` | `array` | 是 | 业务侧最终消费的对话消息流。结构为 `{"role": "...", "text": "..."}`，第一条可为 assistant 开场白。 |
| `committed_exchanges` | `array` | 是 | 唯一权威对话账本。每一项是一轮用户输入、assistant 回复和播放状态。 |
| `metrics` | `object` | 是 | 本通电话的核心技术指标。只保留商用第一版必要指标。 |

明确不再输出：

| 字段 | 处理方式 | 原因 |
|---|---|---|
| `input_transcripts` | 不保存 | 可由 `committed_exchanges[*].input_transcript` 派生，单独保存会形成第二套事实源。 |
| `output_transcripts` | 不保存 | 可由 `committed_exchanges[*].output_transcript` 派生，单独保存会形成第二套事实源。 |

## 7. prompt 字段说明

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `scene` | `string` | 是 | 提示词场景。可能是 `default`，也可能是业务身份和 persona 组合。 |
| `version` | `string` | 是 | 提示词来源或版本。常见值包括 `inline`、`fallback`、`postgres`。 |
| `content_hash` | `string` | 否 | 提示词内容 hash。用于确认当时实际使用的是哪一版提示词。 |
| `instructions` | `string` | 是 | 本通电话实际使用的业务提示词。 |
| `loaded_at_ms` | `number` | 否 | 提示词加载时间，毫秒时间戳。 |
| `metadata` | `object` | 否 | 业务提示词附加元数据。可能包含 `identityName`、`personaId`、`debtId`、`employee_name` 等。 |

说明：

- 如果使用数据库业务 prompt，`metadata.source` 通常为 `postgres`。
- 如果没有数据库 prompt，可能是 inline/fallback prompt，metadata 可能较少。

## 8. opening 字段说明

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `text` | `string \| null` | 是 | 开场白原文。业务要求 `turns` 第一条包含开场白时必须保存。注意不要输出到普通日志。 |
| `text_hash` | `string \| null` | 是 | 开场白文本 hash。用于追踪播放的是哪段开场白。 |
| `voice` | `string \| null` | 是 | 业务侧选择的开场白音色枚举。没有开场白时为 `null`。 |
| `speaker` | `string \| null` | 是 | 豆包侧 speaker ID。没有开场白时为 `null`。 |
| `playback_frames` | `number` | 是 | 开场白实际下发到电话侧的音频帧数。 |
| `playback_interrupted` | `boolean` | 是 | 开场白是否被用户插话打断。 |

## 9. turns 字段说明

`turns` 是给业务方直接消费的最终结构：

```json
{
  "turns": [
    {
      "role": "assistant",
      "text": "您好，系统显示您还有物业费未缴。"
    },
    {
      "role": "user",
      "text": "我先核对一下金额。"
    }
  ]
}
```

生成规则：

1. 如果存在 `opening.text`，先追加 `{"role": "assistant", "text": opening.text}`。
2. 再按 `committed_exchanges` 顺序追加每轮用户文本：`{"role": "user", "text": input_transcript}`。
3. 再追加每轮 assistant 文本：`{"role": "assistant", "text": output_transcript}`。
4. 空文本不追加。
5. 被打断的 assistant 完整回复也进入 `turns`，与业务确认的“完整回复按已听完处理”口径一致。

`turns` 是派生业务视图，不替代 `committed_exchanges`。

## 10. committed_exchanges 字段说明

`committed_exchanges` 是最重要的字段。它是唯一权威对话账本。

每一项结构：

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `turn_id` | `number` | 是 | 网关本地对话轮次编号。 |
| `status` | `string` | 是 | 本轮最终状态。`completed` 表示完整播放；`interrupted` 表示被用户打断但仍入账。 |
| `question_id` | `string \| null` | 是 | 预留字段。后续可存模型或火山侧用户问题 item ID。当前通常为 `null`。 |
| `reply_id` | `string \| null` | 是 | 预留字段。后续可存模型或火山侧 assistant reply ID。当前通常为 `null`。 |
| `input_transcript` | `string` | 是 | 用户这一轮说的话。 |
| `output_transcript` | `string` | 是 | assistant 这一轮完整回复。即使被打断，也按业务确认保存完整回复。 |
| `heard_output_transcript` | `string` | 是 | 用户实际听到的文本片段。当前打断时默认空字符串，不做不可靠猜测。完整播放时可等于 `output_transcript`。 |
| `played_audio_ms` | `number` | 是 | 本轮 assistant 回复在电话侧大约播放了多久，单位毫秒。用于审计、质检和争议追溯。 |
| `playback_completed` | `boolean` | 是 | 本轮 assistant 回复是否完整播放到电话侧。 |
| `source` | `string` | 是 | 本条记录进入账本的来源。常见值：`playback_completed`、`client_interrupt`。 |
| `created_at_ms` | `number \| null` | 是 | 本条 exchange 写入账本的时间，毫秒时间戳。 |

`status` 取值：

| 值 | 含义 |
|---|---|
| `completed` | assistant 回复已完整播放到电话侧。 |
| `interrupted` | assistant 回复播放中被用户打断，但完整回复仍作为上下文事实入账。 |

`source` 取值：

| 值 | 含义 |
|---|---|
| `playback_completed` | 电话侧播放完成后进入账本。 |
| `client_interrupt` | 用户打断后进入账本。 |

## 11. metrics 字段说明

`metrics` 只保留第一版商用必要指标，用于判断打断链路和对话账本是否可靠。

| 字段 | 类型 | 是否必填 | 说明 |
|---|---|---:|---|
| `interruptions` | `number` | 是 | 本通电话检测到用户打断的次数。 |
| `freeswitch_break_requests` | `number` | 是 | 请求 FreeSWITCH 停止当前播放的次数。 |
| `freeswitch_break_failures` | `number` | 是 | FreeSWITCH 停播失败次数。正常应为 0。 |
| `realtime_interrupt_requests` | `number` | 是 | 请求豆包 realtime session 打断或修正上下文的次数。 |
| `realtime_interrupt_failures` | `number` | 是 | 豆包 realtime 打断请求失败次数。正常应为 0。 |
| `realtime_session_restarts` | `number` | 是 | 豆包 realtime session 热重启次数。正常打断主路径应为 0。 |
| `gateway_history_committed_turns` | `number` | 是 | 网关已入账的对话轮数。应等于 `committed_exchanges.length`。 |
| `gateway_history_completed_turns` | `number` | 是 | 完整播放后入账的轮数。 |
| `gateway_history_interrupted_turns` | `number` | 是 | 被打断但仍入账的轮数。 |
| `gateway_history_abandoned_turns` | `number` | 是 | 被放弃且没有进入账本的轮数。商用主路径应尽量为 0。 |
| `gateway_history_missing_output_turns` | `number` | 是 | 已入账但缺少 assistant 回复文本的轮数。正常应为 0。 |
| `replayed_input_frames` | `number` | 是 | 打断后重放给新 session 的用户音频帧数。当前主路径应为 0。 |
| `turns_started` | `number` | 是 | 模型侧检测到用户开始说话的 turn 数。 |
| `turns_completed` | `number` | 是 | 模型完成的 turn 数。 |
| `turns_failed` | `number` | 是 | 模型失败或取消的 turn 数。正常应尽量为 0。 |

第一版不进入 payload 的调试指标：

| 字段类型 | 例子 | 原因 |
|---|---|---|
| 音频帧和字节计数 | `inbound_frames`、`outbound_bytes` | 更偏底层排障，日志中保留即可。 |
| 播放队列指标 | `max_playback_queue_frames`、`playback_underruns` | 更偏性能调优，第一版不必持久化。 |
| RMS 和相关性指标 | `opening_trigger_rms`、`opening_trigger_best_playback_correlation` | 主要用于开场白插话排查，不属于核心通话结果。 |
| 细粒度播放事件数 | `freeswitch_playback_events`、`freeswitch_queue_completed_events` | 可从日志排查，第一版不作为业务持久化字段。 |
| 重放字节数 | `replayed_input_bytes` | 当前主路径不重放，保留 `replayed_input_frames` 已足够判断是否走了重放路径。 |

## 12. 写入流程

通话结束后：

```text
FreeSWITCH media websocket disconnected
  -> gateway shutdown session
  -> build call result payload
  -> call_result_writer.enqueue_nowait(payload)
  -> background writer insert/upsert PostgreSQL
```

写入方式建议使用 upsert：

```sql
insert into realtime_call_results (
  call_id,
  session_id,
  status,
  connected_at_ms,
  disconnected_at_ms,
  duration_ms,
  committed_turns,
  completed_turns,
  interrupted_turns,
  payload
) values (
  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb
)
on conflict (call_id) do update set
  session_id = excluded.session_id,
  status = excluded.status,
  connected_at_ms = excluded.connected_at_ms,
  disconnected_at_ms = excluded.disconnected_at_ms,
  duration_ms = excluded.duration_ms,
  committed_turns = excluded.committed_turns,
  completed_turns = excluded.completed_turns,
  interrupted_turns = excluded.interrupted_turns,
  payload = excluded.payload,
  updated_at = now();
```

## 13. 失败处理

第一版建议：

1. `enqueue_nowait(payload)` 必须很快返回，不能阻塞实时通话结束流程。
2. writer 队列满时，打印 `call_result_enqueue_failed` warning。
3. PostgreSQL 写入失败时，打印 warning，并保留日志中的 call_id。
4. 第一版不做无限重试，避免数据库异常拖垮网关。
5. 后续如果要保证强一致，可增加本地 JSONL fallback 或消息队列。

## 14. 需要技术经理确认的问题

请重点确认以下事项：

1. 是否接受第一版只建 `realtime_call_results` 一张主表。
2. 是否接受完整通话结果存入 `payload jsonb`。
3. 是否确认 `committed_exchanges` 是唯一权威对话账本。
4. 是否确认 `turns` 是业务侧最终消费结构，第一条包含 assistant 开场白。
5. 是否确认保存 `opening.text` 开场白原文，但不输出到普通日志。
6. 是否确认不保存 `input_transcripts/output_transcripts` 两个派生数组。
7. 是否确认第一版 `metrics` 只保留本文列出的核心字段。
8. 是否需要在第一版表字段中增加 `external_call_id`、`destination`、`caller_id_number` 等外呼控制字段。
9. 是否需要把 `prompt.instructions` 全量保存到数据库，还是只保存 `content_hash` 和 metadata。
10. 是否需要对 `committed_exchanges.input_transcript/output_transcript` 做脱敏或加密。
11. 是否接受 PostgreSQL 写入失败只记录 warning，不阻塞通话主流程。
12. 是否需要本地 JSONL fallback，防止数据库短暂不可用时丢失 call result。

## 15. 建议确认结论

建议第一版按以下口径执行：

```text
表结构：一张 realtime_call_results 主表。
事实源：committed_exchanges。
业务视图：turns，第一条包含 assistant 开场白。
存储方式：常用字段单独成列，完整结果放 payload jsonb。
派生数组：不保存 input_transcripts/output_transcripts。
metrics：只保存核心商用指标。
失败处理：不阻塞主流程，写入失败先打 warning。
后续增强：需要强可靠时再加 JSONL fallback 或消息队列。
```
