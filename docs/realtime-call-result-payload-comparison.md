# Call Result Payload 原始结构与 PostgreSQL 持久化结构比对

日期：2026-05-22

本文用于比对当前代码里 `_build_call_result_payload()` 生成的原始 payload，以及建议写入 PostgreSQL 的精简 payload。

## 1. 结论

当前原始 payload 已经具备完整通话结果，但 `metrics` 偏调试和运行诊断，字段较多。

PostgreSQL 第一版建议：

1. 顶层字段基本保留。
2. `turns` 保留，作为业务侧最终消费的对话消息流。
3. `committed_exchanges` 完整保留。
4. `input_transcripts` / `output_transcripts` 不保留，目前代码已删除。
5. `metrics` 只保留商用第一版需要长期查询和质检判断的核心字段。
6. 其他底层调试指标继续留在日志里，不进入数据库 payload。

## 2. 顶层字段比对

| 字段 | 当前原始 payload | PostgreSQL 建议 payload | 说明 |
|---|---:|---:|---|
| `call_id` | 保留 | 保留 | 本通电话唯一 ID。 |
| `session_id` | 保留 | 保留 | 网关实时媒体 session ID。 |
| `status` | 保留 | 保留 | 通话结果状态。 |
| `connected_at_ms` | 保留 | 保留 | 媒体接入时间。 |
| `disconnected_at_ms` | 保留 | 保留 | 媒体断开时间。 |
| `duration_ms` | 保留 | 保留 | 媒体连接持续时长。 |
| `prompt` | 保留 | 保留 | 本通电话使用的提示词快照。 |
| `opening` | 保留 | 保留 | 开场白播放信息。 |
| `turns` | 保留 | 保留 | 业务侧最终消费结构，形如 `{"turns":[{"role":"assistant","text":"..."}]}`。 |
| `committed_exchanges` | 保留 | 保留 | 唯一权威对话账本，必须完整保留。 |
| `metrics` | 保留，字段较多 | 保留，字段精简 | 原始字段偏多，入库版只保留核心指标。 |
| `input_transcripts` | 已删除 | 不保留 | 可从 `committed_exchanges` 派生，避免第二套事实源。 |
| `output_transcripts` | 已删除 | 不保留 | 可从 `committed_exchanges` 派生，避免第二套事实源。 |

## 3. committed_exchanges 比对

`committed_exchanges` 是唯一权威对话账本。当前原始 payload 和 PostgreSQL 建议 payload 都完整保留该结构。

| 字段 | 当前原始 payload | PostgreSQL 建议 payload | 说明 |
|---|---:|---:|---|
| `turn_id` | 保留 | 保留 | 网关本地对话轮次编号。 |
| `status` | 保留 | 保留 | 本轮状态，`completed` 或 `interrupted`。 |
| `question_id` | 保留 | 保留 | 预留模型侧用户问题 ID。当前通常为 `null`。 |
| `reply_id` | 保留 | 保留 | 预留模型侧 assistant 回复 ID。当前通常为 `null`。 |
| `input_transcript` | 保留 | 保留 | 用户这一轮说的话。 |
| `output_transcript` | 保留 | 保留 | assistant 这一轮完整回复。打断时也保存完整回复。 |
| `heard_output_transcript` | 保留 | 保留 | 用户实际听到的文本片段。当前打断时默认空字符串。 |
| `played_audio_ms` | 保留 | 保留 | 本轮 assistant 回复已播放时长。 |
| `playback_completed` | 保留 | 保留 | 本轮 assistant 回复是否完整播放。 |
| `source` | 保留 | 保留 | 本条记录入账来源，如 `playback_completed`、`client_interrupt`。 |
| `created_at_ms` | 保留 | 保留 | 本条记录写入账本的时间。 |

结论：`committed_exchanges` 不做精简。

## 4. metrics 字段比对

### 4.1 建议保留入库的 metrics

这些字段建议进入 PostgreSQL payload，因为它们直接回答商用关心的问题：有没有打断、打断是否成功、上下文账本是否可靠、模型 turn 是否失败。

| 字段 | 当前原始 payload | PostgreSQL 建议 payload | 保留原因 |
|---|---:|---:|---|
| `interruptions` | 保留 | 保留 | 判断本通电话发生了几次用户打断。 |
| `freeswitch_break_requests` | 保留 | 保留 | 判断电话侧停播控制是否触发。 |
| `freeswitch_break_failures` | 保留 | 保留 | 判断电话侧停播是否失败。 |
| `realtime_interrupt_requests` | 保留 | 保留 | 判断是否向豆包 realtime session 发起打断请求。 |
| `realtime_interrupt_failures` | 保留 | 保留 | 判断豆包 realtime 打断是否失败。 |
| `realtime_session_restarts` | 保留 | 保留 | 判断是否发生热重启。正常打断主路径应为 0。 |
| `gateway_history_committed_turns` | 保留 | 保留 | 判断入账总轮数。 |
| `gateway_history_completed_turns` | 保留 | 保留 | 判断完整播放入账轮数。 |
| `gateway_history_interrupted_turns` | 保留 | 保留 | 判断打断入账轮数。 |
| `gateway_history_abandoned_turns` | 保留 | 保留 | 判断是否有轮次被丢弃且未入账。商用主路径应尽量为 0。 |
| `gateway_history_missing_output_turns` | 保留 | 保留 | 判断是否有入账轮次缺 assistant 回复。正常应为 0。 |
| `replayed_input_frames` | 保留 | 保留 | 判断是否走了音频重放路径。当前主路径应为 0。 |
| `turns_started` | 保留 | 保留 | 判断模型侧检测到用户开始说话的次数。 |
| `turns_completed` | 保留 | 保留 | 判断模型完成 turn 数。 |
| `turns_failed` | 保留 | 保留 | 判断模型失败或取消 turn 数。 |

### 4.2 建议不入库、仅保留日志的 metrics

这些字段当前原始 payload 中有，但 PostgreSQL 第一版建议不持久化。原因是它们更偏底层排障、播放调优或开场白插话算法诊断，不是第一版商用长期查询字段。

| 字段 | 当前原始 payload | PostgreSQL 建议 payload | 不入库原因 |
|---|---:|---:|---|
| `inbound_frames` | 保留 | 不保留 | 电话上行音频帧数，偏底层链路排查。 |
| `inbound_bytes` | 保留 | 不保留 | 电话上行音频字节数，偏底层链路排查。 |
| `streamed_input_bytes` | 保留 | 不保留 | 发送给模型的输入音频字节数，偏性能和排障。 |
| `outbound_frames` | 保留 | 不保留 | 下行播放帧数，偏音频链路排查。 |
| `outbound_bytes` | 保留 | 不保留 | 下行播放字节数，偏音频链路排查。 |
| `invalid_frame_count` | 保留 | 不保留 | 非法音频帧计数，先保留日志即可。 |
| `local_barge_in_events` | 保留 | 不保留 | 本地插话检测事件，目前主路径依赖 server VAD。 |
| `dropped_playback_frames` | 保留 | 不保留 | 打断时丢弃的播放帧数，偏播放队列调试。 |
| `dropped_stale_frames` | 保留 | 不保留 | 迟到旧音频丢弃数，偏异常排查。可后续按需要补入。 |
| `playback_underruns` | 保留 | 不保留 | 播放 underrun 次数，偏性能调优。 |
| `max_playback_queue_frames` | 保留 | 不保留 | 播放队列峰值，偏性能调优。 |
| `max_playback_send_gap_ms` | 保留 | 不保留 | 播放发送最大间隔，偏音频播放质量调试。 |
| `playback_send_gap_overruns` | 保留 | 不保留 | 播放发送间隔超限次数，偏音频播放质量调试。 |
| `freeswitch_playback_events` | 保留 | 不保留 | FreeSWITCH 播放事件数，偏事件订阅排障。 |
| `freeswitch_queue_completed_events` | 保留 | 不保留 | FreeSWITCH 队列完成事件数，偏播放完成诊断。 |
| `context_repair_requests` | 保留 | 不保留 | 与 `realtime_interrupt_requests` 部分重叠，第一版可不入库。 |
| `replayed_input_bytes` | 保留 | 不保留 | 保留 `replayed_input_frames` 足够判断是否重放。 |
| `turns_committed` | 保留 | 不保留 | 与 `gateway_history_committed_turns` 容易混淆，第一版不入库。 |
| `opening_trigger_rms` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_rms_min` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_rms_max` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_rms_avg` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_best_playback_correlation` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_best_playback_frame` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_best_playback_rms` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |
| `opening_trigger_last_playback_age_ms` | 保留 | 不保留 | 开场白插话检测细节，偏算法调试。 |

## 5. 当前原始 payload 示例

当前代码原始结构如下：

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
    "instructions": "当前电话业务提示词"
  },
  "opening": {
    "text": null,
    "text_hash": null,
    "voice": null,
    "speaker": null,
    "playback_frames": 0,
    "playback_interrupted": false
  },
  "turns": [
    {
      "role": "user",
      "text": "用户说的话"
    },
    {
      "role": "assistant",
      "text": "assistant 完整回复"
    }
  ],
  "committed_exchanges": [
    {
      "turn_id": 1,
      "status": "completed",
      "question_id": null,
      "reply_id": null,
      "input_transcript": "用户说的话",
      "output_transcript": "assistant 完整回复",
      "heard_output_transcript": "用户实际听到的文本",
      "played_audio_ms": 2808,
      "playback_completed": true,
      "source": "playback_completed",
      "created_at_ms": 1779433570447
    }
  ],
  "metrics": {
    "inbound_frames": 0,
    "inbound_bytes": 0,
    "streamed_input_bytes": 0,
    "outbound_frames": 0,
    "outbound_bytes": 0,
    "invalid_frame_count": 0,
    "interruptions": 0,
    "local_barge_in_events": 0,
    "dropped_playback_frames": 0,
    "dropped_stale_frames": 0,
    "playback_underruns": 0,
    "max_playback_queue_frames": 0,
    "max_playback_send_gap_ms": 0,
    "playback_send_gap_overruns": 0,
    "freeswitch_playback_events": 0,
    "freeswitch_queue_completed_events": 0,
    "freeswitch_break_requests": 0,
    "freeswitch_break_failures": 0,
    "realtime_interrupt_requests": 0,
    "realtime_interrupt_failures": 0,
    "context_repair_requests": 0,
    "realtime_session_restarts": 0,
    "gateway_history_committed_turns": 0,
    "gateway_history_abandoned_turns": 0,
    "gateway_history_completed_turns": 0,
    "gateway_history_interrupted_turns": 0,
    "gateway_history_missing_output_turns": 0,
    "replayed_input_frames": 0,
    "replayed_input_bytes": 0,
    "turns_started": 0,
    "turns_committed": 0,
    "turns_completed": 0,
    "turns_failed": 0,
    "opening_trigger_rms": null,
    "opening_trigger_rms_min": null,
    "opening_trigger_rms_max": null,
    "opening_trigger_rms_avg": null,
    "opening_trigger_best_playback_correlation": null,
    "opening_trigger_best_playback_frame": null,
    "opening_trigger_best_playback_rms": null,
    "opening_trigger_last_playback_age_ms": null
  }
}
```

## 6. PostgreSQL 建议 payload 示例

建议入库结构如下：

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
    "instructions": "当前电话业务提示词"
  },
  "opening": {
    "text": null,
    "text_hash": null,
    "voice": null,
    "speaker": null,
    "playback_frames": 0,
    "playback_interrupted": false
  },
  "turns": [
    {
      "role": "user",
      "text": "用户说的话"
    },
    {
      "role": "assistant",
      "text": "assistant 完整回复"
    }
  ],
  "committed_exchanges": [
    {
      "turn_id": 1,
      "status": "completed",
      "question_id": null,
      "reply_id": null,
      "input_transcript": "用户说的话",
      "output_transcript": "assistant 完整回复",
      "heard_output_transcript": "用户实际听到的文本",
      "played_audio_ms": 2808,
      "playback_completed": true,
      "source": "playback_completed",
      "created_at_ms": 1779433570447
    }
  ],
  "metrics": {
    "interruptions": 0,
    "freeswitch_break_requests": 0,
    "freeswitch_break_failures": 0,
    "realtime_interrupt_requests": 0,
    "realtime_interrupt_failures": 0,
    "realtime_session_restarts": 0,
    "gateway_history_committed_turns": 1,
    "gateway_history_completed_turns": 1,
    "gateway_history_interrupted_turns": 0,
    "gateway_history_abandoned_turns": 0,
    "gateway_history_missing_output_turns": 0,
    "replayed_input_frames": 0,
    "turns_started": 1,
    "turns_completed": 1,
    "turns_failed": 0
  }
}
```

## 7. 需要确认的取舍

请技术经理重点确认：

1. 是否同意 `committed_exchanges` 不做任何精简。
2. 是否同意 `metrics` 入库时做精简。
3. 是否同意调试型 metrics 只保留日志，不进入 PostgreSQL payload。
4. 是否同意 `input_transcripts` / `output_transcripts` 不恢复。
5. 是否同意 `turns` 作为业务侧最终消费结构保留。
6. 是否同意 `opening.text` 保存开场白原文，但不输出到普通日志。
7. 是否需要把 `dropped_stale_frames` 放回 PostgreSQL payload。它对排查旧音频迟到有帮助，但不是第一版必需。
8. 是否需要把 `context_repair_requests` 放回 PostgreSQL payload。它和 `realtime_interrupt_requests` 接近，第一版可不保留。

## 8. 建议结论

建议 PostgreSQL 第一版采用精简 payload。

理由：

1. 对话事实不丢，因为 `committed_exchanges` 完整保留。
2. 商用核心判断不丢，因为打断、停播、模型打断、热重启、账本异常等核心 metrics 保留。
3. 数据库 payload 更干净，避免长期保存大量底层调试字段。
4. 如果后续线上排障证明某个调试指标必须长期查询，可以再加回 payload。
