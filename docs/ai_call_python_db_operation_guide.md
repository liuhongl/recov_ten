# 智能外呼 Python 侧数据库操作说明

更新时间：2026-05-22

## 1. 文档范围

本文主要说明智能外呼 Python 项目如何维护数据库表 `public.call_record`。
另外，Python 在接收 Java 外呼任务后，会按 `debtId` 只读查询 `debt_record.debtor_phone` 作为被叫号码；该查询不改变 `debt_record`。

本文不包含：

```text
Python 接入 MQ。
Java 流程推进逻辑。
语义分析。
前端查询接口。
```

说明：Python 通知 Java 流程节点终态属于流程 callback 集成范围，不在本文展开；对应契约见 `docs/flow_external_callback_integration.md`。

当前职责边界：

```text
Java 负责初始化 call_record，并在调用 Python 外呼时传入流程 callback 所需的 taskId。
Python 外呼项目负责外呼开始后的通话状态、转写结果和流程 callback 触发。
语义分析由其他模块处理，不属于本文档范围。
```

也就是：

```text
Java 插入 call_record.status = '0'
Python 更新 status / started_at / finished_at / transcript
Python 不写 analysis_result
Python 不维护 analysis_status = '1' / '2' / '3'
Python 不直接写 Java 流程表
```

## 2. Python 入参

Python 执行数据库更新时，至少需要拿到以下字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `callId` | string | 是 | 通话 ID，对应 `call_record.id` |
| `debtId` | string | 是 | 债务记录 ID，对应 `call_record.debt_id` |
| `identityName` | string | 是 | 外呼身份名称，例如 `项目员工`、`企业客服` |
| `taskId` | string | 流程回调开启时必填 | Java 流程节点执行记录 ID，用于 callback 关联节点。 |
| `tenantId` | string | 建议 | 租户编号，用于 Java 消费 callback 时校验。 |

注意：

```text
callId 和 debtId 对外都按字符串处理。
Python 写数据库时再按 bigint 绑定参数。
字段名是 debtId，不是 debtld。
callId 对应 call_record.id，主键唯一，Python 后续只按 callId 定位记录。
当 `flow_callback.enabled=true` 时，taskId 缺失会被 Python 拒绝，避免外呼执行后无法推进流程。
```

## 3. 表字段职责

`call_record` 中，Java 初始化以下字段：

| 字段 | Java 初始化规则 |
| --- | --- |
| `id` | `callId` |
| `debt_id` | `debtId` |
| `tenant_id` | Java 根据当前租户初始化，Python 不需要传入或修改 |
| `status` | 固定初始化为 `'0'` |
| `analysis_status` | 固定初始化为 `'0'`，等待后续语义模块处理 |
| `analysis_retry_count` | 固定初始化为 `0` |
| `create_time` | 初始化时间 |
| `update_time` | 初始化时间 |

Python 外呼项目只维护以下字段：

| 字段 | Python 维护规则 |
| --- | --- |
| `status` | 通话 / 转写状态 |
| `started_at` | 通话开始时间 |
| `finished_at` | 通话结束时间 |
| `transcript` | 通话转写 JSON 字符串 |
| `update_time` | 每次更新时刷新 |

Python 外呼项目不应修改：

```text
analysis_status
analysis_result
analysis_error
analysis_retry_count
analysis_started_at
analysis_finished_at
debt_record
recov_flow_instance
recov_node_execution_record
recov_flow_mq_outbox
```

说明：`debt_record` 不由 Python 写入或更新。Python 只允许按 `debtId` 读取 `debtor_phone`，用于解析本次外呼的被叫号码。

## 4. 通话状态 status

| 值 | 含义 | 维护方 |
| --- | --- | --- |
| `0` | 未开始 | Java 初始化 |
| `1` | 进行中 | Python 开始外呼后更新 |
| `2` | 外呼失败 | Python 判断外呼失败后更新 |
| `3` | 未接听 | Python 判断未接听后更新 |
| `4` | 转写完成 | Python 写入 transcript 后更新 |

说明：

```text
status = '4' 只表示转写完成。
status = '4' 不表示语义分析成功。
后续语义模块会基于 status = '4' 且 analysis_status = '0' 的记录继续处理。
```

## 5. 更新前校验

Python 每次更新前，应先按 `callId` 查询 Java 已初始化的记录。

```sql
SELECT
  id,
  debt_id,
  status,
  transcript
FROM public.call_record
WHERE id = :call_id
LIMIT 1;
```

校验规则：

```text
查不到记录：不要创建 call_record，记录错误，交由任务失败处理。
debt_id 与 debtId 不一致：不要更新。
status = '2' / '3' / '4' 时不要覆盖已有终态，除非是明确的人工重跑。
```

说明：

```text
Java 已负责初始化 call_record。
Python 不要在找不到记录时自行 INSERT，避免绕过 Java 的流程实例和租户上下文。
```

## 6. 被叫号码查询

Java 调 Python `/calls` 时不需要传 `destination`。Python 使用 `debtId` 查询债务人的手机号：

```sql
SELECT debtor_phone
FROM public.debt_record
WHERE id = :debt_id
LIMIT 1;
```

规则：

```text
debtId 缺失或不是 bigint：不发起外呼。
查不到 debt_record：不发起外呼。
debtor_phone 为空：不发起外呼。
PostgreSQL 不可用或查询超时：返回外部资源暂时不可用，让 Java 按重试策略处理。
```

## 7. 标准更新 SQL

以下 SQL 均要求 Python 使用参数化查询，不要拼接 SQL。

所有更新都必须带上：

```text
id = :call_id
```

`callId` 对应 `call_record.id`，主键唯一，因此不需要额外带 `tenant_id` 条件。

### 7.1 通话开始

外呼供应商确认开始拨打或通话开始时：

```sql
UPDATE public.call_record
SET status = '1',
    started_at = :started_at,
    update_time = CURRENT_TIMESTAMP
WHERE id = :call_id
  AND status IN ('0', '1');
```

### 7.2 外呼失败

连接供应商失败、号码无效、拨打失败、ASR 无法产出有效转写等无法形成有效通话结果的场景：

```sql
UPDATE public.call_record
SET status = '2',
    finished_at = :finished_at,
    update_time = CURRENT_TIMESTAMP
WHERE id = :call_id
  AND status IN ('0', '1');
```

当前 `call_record` 没有独立的外呼失败原因字段。外呼失败原因不要写入 `analysis_error`，Python 可记录在自己的任务表或日志中。

### 7.3 未接听

用户未接听、关机、无人应答、无法产生有效通话内容：

```sql
UPDATE public.call_record
SET status = '3',
    finished_at = :finished_at,
    update_time = CURRENT_TIMESTAMP
WHERE id = :call_id
  AND status IN ('0', '1');
```

### 7.4 转写完成

通话完成且 `transcript` 已生成后：

```sql
UPDATE public.call_record
SET status = '4',
    finished_at = :finished_at,
    transcript = :transcript_json,
    update_time = CURRENT_TIMESTAMP
WHERE id = :call_id
  AND status IN ('1', '4');
```

要求：

```text
transcript_json 必须是合法 JSON 字符串。
status = '4' 只表示转写完成。
Python 不要在这里写 analysis_status = '1'。
Python 不要在这里写 analysis_result。
```

## 8. transcript JSON

`transcript` 存储在 `call_record.transcript`，字段类型是 text，内容必须是 JSON 字符串。

推荐结构：

```json
{
  "version": "1.0",
  "provider": "python-ai-call",
  "callId": "990000000000032001",
  "turns": [
    {
      "seq": 1,
      "role": "assistant",
      "text": "您好，这里是灵辰智能外呼，提醒您关注当前账款处理进度。",
      "startMs": 0,
      "endMs": 1800,
      "confidence": 0.96
    },
    {
      "seq": 2,
      "role": "user",
      "text": "我知道了，明天上午我会处理。",
      "startMs": 2000,
      "endMs": 3600,
      "confidence": 0.91
    }
  ]
}
```

要求：

```text
turns 按时间顺序排列。
role 使用 assistant / user。
text 不为空。
startMs / endMs 使用毫秒。
```

## 9. 事务和幂等

### 9.1 事务要求

每次状态更新都应在数据库事务中完成。

转写完成时至少保证：

```text
status = '4'
finished_at
transcript
update_time
```

以上字段在同一事务中提交。

### 9.2 幂等要求

Python 必须以 `callId` 做幂等键。

推荐 Python 自己维护本地任务表，并对 `callId` 建唯一约束，避免重复外呼。

重复任务处理：

| 当前 call_record 状态 | Python 处理 |
| --- | --- |
| `status='0'` | 可开始外呼 |
| `status='1'` | 说明已在执行，不重复创建外呼 |
| `status='2'` | 外呼失败终态，不覆盖 |
| `status='3'` | 未接听终态，不覆盖 |
| `status='4'` | 转写完成终态，不覆盖，除非是明确人工重跑 |

SQL 更新影响行数为 `0` 时，不要盲目重试覆盖。应重新查询当前记录状态，再决定是否已经终态或状态不允许。

## 10. 最小验收标准

| 场景 | 表状态 |
| --- | --- |
| Java 初始化后 | `status='0'` |
| Python 开始外呼 | `status='1'`、`started_at IS NOT NULL` |
| 外呼失败 | `status='2'`、`finished_at IS NOT NULL` |
| 未接听 | `status='3'`、`finished_at IS NOT NULL` |
| 转写完成 | `status='4'`、`finished_at IS NOT NULL`、`transcript IS NOT NULL` |

核心检查 SQL：

```sql
SELECT
  id,
  debt_id,
  tenant_id,
  status,
  started_at,
  finished_at,
  transcript,
  analysis_status,
  analysis_result
FROM public.call_record
WHERE id = :call_id;
```

转写完成后应满足：

```text
status = '4'
transcript IS NOT NULL
analysis_status = '0'
analysis_result IS NULL
```
