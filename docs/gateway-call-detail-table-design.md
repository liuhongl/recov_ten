# 网关通话详情表设计确认文档

确认日期：2026-05-25

本文用于给技术经理确认：SIP 实时语音网关是否新增一张通话详情表，以及第一版字段、payload 内容、写入时机和边界。

## 1. 设计目标

新增通话详情表的目标不是替代现有 `public.call_record`，而是补齐网关侧的通话事实记录。

第一版需要回答：

1. 这通电话关联哪个 Java / 业务通话记录。
2. 这通电话拨给谁、最终是什么状态。
3. 什么时候开始拨号、什么时候接通、什么时候结束。
4. 总耗时和真实通话时长是多少。
5. 忙线、拒接、无人接听、线路失败等原因是什么。
6. 如已进入实时媒体链路，能保留转写、对话账本、开场白、核心指标等复盘信息。

第一版不追求把所有 FreeSWITCH、SIP、音频和模型调试字段都铺成数据库列。常用查询字段单独成列，完整通话事实放入 `payload jsonb`。

## 2. 当前背景

当前 Python 网关已有内存态通话记录，`GET /calls/{call_id}` 可以返回：

- `created_at_ms`
- `started_at_ms`
- `ringing_at_ms`
- `answered_at_ms`
- `media_connected_at_ms`
- `media_disconnected_at_ms`
- `completed_at_ms`
- `elapsed_ms`
- `answer_latency_ms`
- `ringing_ms`
- `talk_duration_ms`
- `hangup_cause`
- `sip_status`
- `failure_reason`

但这些记录保存在进程内存中，网关重启后会丢失。

现有 `public.call_record` 由 Java 侧初始化，Python 只更新：

- `status`
- `started_at`
- `finished_at`
- `transcript`
- `update_time`

因此，当前数据库不能完整保存接通时间、真实通话时长、SIP 状态、挂断原因、媒体会话结果和对话账本。

## 3. 推荐新增表

推荐表名：

```sql
gateway_call_detail
```

表含义：一条记录对应一次网关外呼尝试。无论最终是接通、无人接听、忙线、拒接、拨号失败还是媒体异常，都应该尽量写入或更新这张表。

## 4. 第一版表结构

```sql
create table gateway_call_detail (
  id bigserial primary key,

  gateway_call_id text not null unique,
  business_call_id text,
  tenant_id text,
  debt_id text,
  destination text,

  status text not null,

  created_at timestamptz,
  dial_started_at timestamptz,
  answered_at timestamptz,
  finished_at timestamptz,

  total_duration_ms integer,
  talk_duration_ms integer,

  hangup_cause text,
  sip_status text,
  failure_reason text,

  transcript_json jsonb,
  payload jsonb not null default '{}'::jsonb,

  created_db_at timestamptz not null default now(),
  updated_db_at timestamptz not null default now()
);
```

## 5. 字段说明

| 字段 | 含义 | 说明 |
|---|---|---|
| `id` | 数据库自增主键 | 仅用于数据库内部定位，不作为业务幂等键。 |
| `gateway_call_id` | Python / FreeSWITCH 内部通话 ID | 当前代码中的网关 `call_id`，也是 FreeSWITCH `origination_uuid`。唯一。 |
| `business_call_id` | Java 业务通话 ID | 通常对应 `public.call_record.id`，来自请求中的 `callId`。 |
| `tenant_id` | 租户 ID | 来自请求上下文 `tenantId`，用于隔离查询和排查。 |
| `debt_id` | 债务记录 ID | 来自请求上下文 `debtId`，用于关联 `debt_record`。 |
| `destination` | 被叫号码 | 已确认允许明文入库。 |
| `status` | 网关最终状态 | 建议取值见第 7 节。 |
| `created_at` | 外呼任务创建时间 | Python 网关接收 `/calls` 并创建内存记录的时间。 |
| `dial_started_at` | 开始拨号时间 | Python 开始向 FreeSWITCH 发起 originate 的时间。 |
| `answered_at` | 对端接通时间 | 收到 FreeSWITCH `CHANNEL_ANSWER` 的时间。未接通时为空。 |
| `finished_at` | 通话结束时间 | 收到挂断终态、拨号失败或内部失败的时间。 |
| `total_duration_ms` | 总耗时 | 通常为 `finished_at - dial_started_at`，包含拨号和振铃时间。 |
| `talk_duration_ms` | 真实通话时长 | 通常为 `finished_at - answered_at`。未接通时为空。 |
| `hangup_cause` | FreeSWITCH 挂断原因 | 例如 `NORMAL_CLEARING`、`USER_BUSY`、`CALL_REJECTED`、`NO_ANSWER`。 |
| `sip_status` | SIP 状态码 | 例如 `486`、`603`、`408`、`480`、`503`。可为空。 |
| `failure_reason` | 归一化失败原因 | 网关根据 hangup cause / SIP 状态归一化后的失败原因。成功通话为空。 |
| `transcript_json` | 简化转写 JSON | 可保存 `{"turns":[...]}`，便于业务直接查看；如果仍以 `call_record.transcript` 为唯一业务转写源，此字段可为空。 |
| `payload` | 完整通话事实 JSON | 保存请求、时间线、结果、媒体、对话、prompt、opening、metrics 等复盘信息。 |
| `created_db_at` | 数据库创建时间 | 数据库记录首次插入时间。 |
| `updated_db_at` | 数据库更新时间 | 每次 upsert 更新。 |

## 6. 为什么这些字段够第一版

第一版表列只保留经常查询、筛选、统计的字段：

- 按业务记录查：`business_call_id`
- 按租户 / 债务查：`tenant_id`、`debt_id`
- 按号码查：`destination`
- 按结果统计：`status`、`failure_reason`
- 按时间统计：`created_at`、`dial_started_at`、`answered_at`、`finished_at`
- 按时长统计：`total_duration_ms`、`talk_duration_ms`

以下字段第一版不单独建列，放入 `payload`：

| 字段类型 | 原因 |
|---|---|
| `caller_id_name`、`caller_id_number` | 主要排障或审计使用，不是第一版高频统计字段。 |
| `endpoint`、`requested_endpoint` | FreeSWITCH / SIP 线路排障字段，适合放 payload。 |
| `dialplan_extension`、`dialplan_context` | FreeSWITCH 技术字段，适合放 payload。 |
| `ringing_at`、`ringing_duration_ms` | 有价值，但第一版非必需；需要接通率漏斗分析时再升为列。 |
| `media_connected_at`、`media_duration_ms` | 媒体链路排障字段，先放 payload。 |
| `last_event_name`、`last_event_at` | 复盘字段，先放 payload。 |
| `freeswitch_reply`、`error` | 可能较长、不稳定，先放 payload。 |
| `metrics_json` | 与 `payload.metrics` 重复，第一版不单独建列。 |

## 7. status 建议取值

| status | 含义 | 典型来源 |
|---|---|---|
| `queued` | 已创建但未开始拨号 | `/calls` 已受理但 originate worker 尚未开始。可选是否入库。 |
| `originating` | 正在发起拨号 | 开始执行 originate。 |
| `ringing` | 已振铃 | 收到 `CHANNEL_PROGRESS` 或 `CHANNEL_PROGRESS_MEDIA`。 |
| `answered` | 已接通 | 收到 `CHANNEL_ANSWER`。 |
| `media_connected` | 实时媒体已接入 | FreeSWITCH media WebSocket 连接到 Python。 |
| `completed` | 正常结束 | 接通后正常挂断，通常 `hangup_cause=NORMAL_CLEARING`。 |
| `busy` | 忙线或拒接 | `USER_BUSY`、`CALL_REJECTED` 或对应 SIP 状态。 |
| `no_answer` | 无人接听 | `NO_ANSWER`、SIP `408` / `480` 等。 |
| `canceled` | 呼叫取消 | 未接通前被取消或 `ORIGINATOR_CANCEL`。 |
| `failed` | 失败 | 线路、系统、参数、上游异常等。 |
| `hangup_failed` | 主动挂断请求失败 | `uuid_kill` 返回错误。 |

表中的 `status` 建议保存最终状态；进行中状态是否实时 upsert，可由实现阶段决定。

## 8. 推荐索引

```sql
create unique index gateway_call_detail_gateway_call_id_idx
  on gateway_call_detail (gateway_call_id);

create index gateway_call_detail_business_call_id_idx
  on gateway_call_detail (business_call_id);

create index gateway_call_detail_tenant_created_idx
  on gateway_call_detail (tenant_id, created_db_at desc);

create index gateway_call_detail_status_created_idx
  on gateway_call_detail (status, created_db_at desc);
```

说明：

- `gateway_call_id` 用于网关幂等 upsert。
- `business_call_id` 用于从 Java `call_record` 反查网关详情。
- `tenant_id + created_db_at` 用于租户维度查询。
- `status + created_db_at` 用于失败率、接通率统计。

第一版不建议默认给 `destination` 建索引。号码明文入库用于排查即可；如果后续运营后台确实高频按号码查历史，再补充：

```sql
create index gateway_call_detail_destination_created_idx
  on gateway_call_detail (destination, created_db_at desc);
```

## 9. payload 结构

`payload` 是完整通话事实快照，第一版建议结构如下：

```json
{
  "schema_version": "1.0",
  "ids": {
    "gateway_call_id": "python-freeswitch-uuid",
    "business_call_id": "call_record.id",
    "task_id": "flow-task-id",
    "tenant_id": "tenant-id",
    "debt_id": "debt-id",
    "external_call_id": "external-call-id"
  },
  "request": {
    "destination": "13800138000",
    "caller_id_number": "9000",
    "caller_id_name": "AI_Assistant",
    "endpoint": "sofia/gateway/demo/13800138000",
    "requested_endpoint": "sofia_contact:*/1000",
    "dialplan_extension": "9199",
    "dialplan_context": "default",
    "originate_timeout_seconds": 30
  },
  "timeline": {
    "created_at_ms": 1779433560000,
    "dial_started_at_ms": 1779433560100,
    "originate_completed_at_ms": 1779433560500,
    "ringing_at_ms": 1779433562000,
    "answered_at_ms": 1779433568000,
    "media_connected_at_ms": 1779433568300,
    "media_disconnected_at_ms": 1779433620000,
    "finished_at_ms": 1779433620100,
    "last_event_at_ms": 1779433620100
  },
  "durations": {
    "total_duration_ms": 60000,
    "originate_elapsed_ms": 400,
    "answer_latency_ms": 7900,
    "ringing_duration_ms": 6000,
    "talk_duration_ms": 53200,
    "media_duration_ms": 51700
  },
  "result": {
    "status": "completed",
    "hangup_cause": "NORMAL_CLEARING",
    "sip_status": "200",
    "sip_reason": null,
    "failure_reason": null,
    "last_event_name": "CHANNEL_HANGUP_COMPLETE",
    "freeswitch_reply": "+OK ...",
    "error": null
  },
  "conversation": {
    "turns": [
      {
        "role": "assistant",
        "text": "您好，请问是张先生吗？"
      },
      {
        "role": "user",
        "text": "是的。"
      }
    ],
    "committed_exchanges": [
      {
        "turn_id": 1,
        "status": "completed",
        "question_id": null,
        "reply_id": null,
        "input_transcript": "是的。",
        "output_transcript": "您好，这边和您核对一下物业费情况。",
        "heard_output_transcript": "您好，这边和您核对一下物业费情况。",
        "played_audio_ms": 2808,
        "playback_completed": true,
        "source": "playback_completed",
        "created_at_ms": 1779433570447
      }
    ]
  },
  "prompt": {
    "scene": "identity:persona",
    "version": "postgres",
    "content_hash": "prompt-content-hash",
    "loaded_at_ms": 1779433560000,
    "metadata": {
      "source": "postgres",
      "identityName": "项目员工",
      "personaId": "7",
      "debtId": "2056563388954320898",
      "employee_name": "物业中心小明"
    }
  },
  "opening": {
    "text": "您好，请问是张先生吗？",
    "text_hash": "opening-text-hash",
    "voice": "female",
    "speaker": "zh_female_xxx",
    "playback_frames": 120,
    "playback_interrupted": false
  },
  "media": {
    "connected": true,
    "session_id": "realtime-session-id",
    "provider": "doubao_s2s"
  },
  "metrics": {
    "committed_turns": 1,
    "completed_turns": 1,
    "interrupted_turns": 0,
    "interruptions": 0,
    "turns_started": 1,
    "turns_completed": 1,
    "turns_failed": 0,
    "freeswitch_break_requests": 0,
    "freeswitch_break_failures": 0,
    "realtime_interrupt_requests": 0,
    "realtime_interrupt_failures": 0,
    "realtime_session_restarts": 0
  }
}
```

## 10. payload 分组说明

| 分组 | 含义 |
|---|---|
| `schema_version` | payload 结构版本，便于后续兼容升级。 |
| `ids` | 网关、业务、流程、租户、债务等关联 ID。 |
| `request` | 本次外呼请求和实际拨号配置。 |
| `timeline` | 全量关键时间点，毫秒时间戳。 |
| `durations` | 由时间线计算出的耗时。 |
| `result` | 最终状态、挂断原因、SIP 状态、错误信息。 |
| `conversation` | 简化对话流和权威对话账本。 |
| `prompt` | 本通电话使用的提示词快照元信息。 |
| `opening` | 开场白文本、音色、播放情况。 |
| `media` | 实时媒体链路信息。 |
| `metrics` | 第一版核心对话和打断指标。 |

## 11. payload 不建议保存的内容

| 内容 | 原因 |
|---|---|
| 原始音频、音频 base64 | 体积过大，不适合存在 PostgreSQL 业务表。 |
| API key、token、签名 secret | 安全风险。 |
| 完整 FreeSWITCH 原始事件 headers | 字段多、价值密度低，容易把结果表变成日志表。 |
| 无限制日志文本 | 会导致 payload 不可控膨胀。 |
| 大段 system prompt 全文 | 可能包含敏感业务规则，第一版建议保存 hash、版本和 metadata；如确需审计，可再确认是否保存全文。 |
| 底层音频帧级指标 | 如 inbound/outbound bytes、RMS、播放队列细节，第一版保留在日志即可。 |

## 12. 失败场景 payload 示例

无人接听时，`conversation` 和 `media` 可以为空：

```json
{
  "schema_version": "1.0",
  "ids": {
    "gateway_call_id": "python-freeswitch-uuid",
    "business_call_id": "2050000000000100001",
    "tenant_id": "1",
    "debt_id": "2056563388954320898"
  },
  "request": {
    "destination": "13800138000",
    "originate_timeout_seconds": 30
  },
  "timeline": {
    "created_at_ms": 1779433560000,
    "dial_started_at_ms": 1779433560100,
    "ringing_at_ms": 1779433562000,
    "finished_at_ms": 1779433590100
  },
  "durations": {
    "total_duration_ms": 30000,
    "answer_latency_ms": null,
    "ringing_duration_ms": 28000,
    "talk_duration_ms": null,
    "media_duration_ms": null
  },
  "result": {
    "status": "no_answer",
    "hangup_cause": "NO_ANSWER",
    "sip_status": "408",
    "failure_reason": "NO_ANSWER"
  },
  "conversation": {
    "turns": [],
    "committed_exchanges": []
  },
  "media": {
    "connected": false,
    "session_id": null
  },
  "metrics": {}
}
```

## 13. 写入策略

推荐使用 upsert，按 `gateway_call_id` 幂等写入。

建议写入时机：

1. `/calls` 创建内存记录后，可插入初始详情记录，状态为 `queued`。
2. 开始 originate 时，更新 `dial_started_at` 和状态。
3. 收到 `CHANNEL_ANSWER` 时，更新 `answered_at`。
4. 收到 `CHANNEL_HANGUP` / `CHANNEL_HANGUP_COMPLETE` 时，更新 `finished_at`、最终状态、耗时、挂断原因。
5. 实时媒体 session 结束并生成转写 payload 后，补充 `transcript_json`、`payload.conversation`、`payload.metrics` 等。

第一版也可以简化为只在终态时 upsert 一次，优点是实现简单；缺点是如果进程在通话中途崩溃，详情表可能没有该通记录。

推荐选择：初始插入 + 关键事件更新 + 终态补全。

## 14. 与 public.call_record 的关系

`public.call_record` 继续作为 Java 业务侧主表，Python 仍按现有约定更新状态和转写：

- `status='1'`：拨打中
- `status='2'`：失败
- `status='3'`：无人接听 / 忙线 / 拒接等未有效接通结果
- `status='4'`：转写完成

`gateway_call_detail` 是 Python 网关侧详情表，用于补充：

- 详细时间线
- 通话时长
- 挂断原因
- SIP 状态
- 媒体会话结果
- 对话账本和核心指标

两表通过 `gateway_call_detail.business_call_id = public.call_record.id` 关联。

## 15. 已明确的设计结论

以下事项不需要再找技术经理确认，按本文设计执行：

1. 表名使用 `gateway_call_detail`。
2. `destination` 明文入库。
3. 第一版只把常用查询字段铺成列，其余细节进入 `payload jsonb`。
4. `transcript_json` 在详情表保存一份简化转写快照，但不替代 `public.call_record.transcript` 的业务主表地位。
5. `payload.prompt` 第一版只保存 `scene`、`version`、`content_hash`、`loaded_at_ms`、`metadata`，不保存完整 system prompt。
6. 写入策略采用“初始插入 + 关键事件更新 + 终态补全”，避免进程中途异常时完全丢失通话详情。
7. 第一版不默认给 `destination` 建索引，后续确认为高频查询再加。

## 16. 技术经理只需确认的事项

以下事项涉及共享数据库、跨系统消费或数据治理，需要技术经理确认：

1. **建表和迁移归属**：是否允许 Python 网关在业务 PostgreSQL 中新增 `gateway_call_detail` 表；DDL 由 Python 项目提供后执行，还是纳入 Java / DBA 的统一迁移体系。
2. **消费方式**：`gateway_call_detail` 是仅供网关排障、对账和后台查询，还是 Java 业务系统也会直接依赖它做页面展示或流程判断。如果 Java 会直接消费，需要同步字段稳定性和查询接口边界。
3. **数据保留和访问控制**：该表会保存明文被叫号码、转写文本、对话账本和挂断原因，需要确认保留周期、查询权限、导出权限和生产环境访问范围。
