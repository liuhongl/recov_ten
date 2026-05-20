# PostgreSQL 业务提示词和开场白设计

## 背景

网关当前已经有 PostgreSQL 连接池入口、实时模型 `PromptSnapshot` 注入点，以及外呼前生成个性化开场白的能力。现阶段需要把 `/calls` 接口传入的业务参数用于数据库查询，生成更贴合催收业务的 system prompt，并在外呼前生成同源业务开场白。

## 采用方案

采用方案 C：收到 `/calls` 创建请求后，在发起 FreeSWITCH originate 之前完成数据库查询、prompt 快照生成和开场白音频生成。

查库时机：

```text
POST /calls
-> 读取 context.identityName、context.personaId、context.debtId
-> 查询 PostgreSQL
-> 渲染 prompt_snapshot
-> 渲染 opening_text
-> 生成 opening_audio
-> 创建外呼记录并发起 originate
-> 用户接通后播放 opening_audio
-> 实时会话使用 prompt_snapshot
```

## 接口参数

`POST /calls` 的 `context` 固定接收以下字段：

```json
{
  "identityName": "业务身份编码",
  "personaId": "策略人设 ID",
  "debtId": "债务记录 ID"
}
```

这三个字段是生成业务 prompt 和业务开场白的必需参数。缺失或为空时，本次外呼不应使用业务化 prompt，也不应生成业务化开场白。

## 数据查询

数字员工身份：

```sql
select name
from call_identity_name
where identity_name = $1
order by random()
limit 1
```

催收策略：

```sql
select strategy_core
from persona_call_strategy
where identity_name = $1 and persona_id = $2
limit 1
```

业主信息：

```sql
select debtor_name, address, debt_amount, debtor_gender, debtor_age
from debt_record
where id = $1
limit 1
```

所有 SQL 必须使用参数化查询，不允许字符串拼接 SQL。

## Prompt 模板

```text
# 角色
你是{{数字员工身份}}，负责通过电话进行合规的逾期费用提醒和还款沟通。

# 催收策略
{{策略}}

# 业主信息
业主姓名：{{debtor_name}}
性别：{{debtor_gender}}
年龄：{{debtor_age}}
逾期金额：{{debt_amount}}
地址：{{address}}

# 沟通规范
1. 只围绕逾期费用提醒、身份确认、还款意愿、还款安排进行沟通。
2. 用户询问无关内容时，简短回应并礼貌拉回当前逾期费用事项。
3. 不得威胁、辱骂、施压、冒充司法或公权力机构。
4. 不得向非本人透露欠款金额、地址等隐私信息。
5. 如果用户表示不是本人，应先确认是否方便转告，不得继续披露债务细节。
```

数据库字段作为业务数据注入模板。实现时需要避免把用户可控字段当作新的系统指令执行。

## 开场白模板

开场白和 prompt 使用同一份数据库快照，避免接通后提示词与开场白信息不一致。

```text
您好，请问是{{debtor_name}}{{称呼}}吗？我是{{数字员工身份}}。
这边来电是想和您确认一下{{address}}相关的逾期费用，目前系统显示待处理金额为{{debt_amount}}元，方便和您核实一下吗？
```

称呼规则：

```text
debtor_gender = 男 -> 先生
debtor_gender = 女 -> 女士
其他或为空 -> 空字符串
```

## 数据快照

每通电话只生成一次业务快照。随机选中的数字员工身份、策略、业主信息、prompt 内容、开场白文本都固定到本次外呼记录中，避免重试或实时会话重连时出现身份漂移。

`PromptSnapshot.metadata` 至少记录：

```json
{
  "source": "postgres",
  "identityName": "...",
  "personaId": "...",
  "debtId": "...",
  "employee_name": "...",
  "opening_text_hash": "..."
}
```

日志不得输出完整姓名、地址、金额、完整 prompt 或完整 opening_text。

## 失败处理

如果 PostgreSQL 未启用、DSN 缺失、连接池创建失败、参数缺失或任一必需记录不存在，则回退到当前默认 prompt 和现有开场白行为。

如果业务方要求“没有业务数据就不允许外呼”，后续可以把失败策略从 fallback 调整为创建外呼失败。本次设计默认优先保证电话链路可用。

如果开场白 TTS 生成失败或超时，沿用当前外呼创建失败语义，不发起外呼。

## 测试

需要新增或调整以下测试：

1. `PostgresPromptStore` 能按 `identityName/personaId/debtId` 查询并渲染 prompt。
2. 查询参数缺失或查不到记录时返回 fallback prompt。
3. 外呼创建时把业务 prompt 快照和开场白绑定到本次 call record。
4. 实时媒体会话使用 call record 中已生成的 prompt 快照，不再接通后重新查库。
5. 开场白文本使用同一份快照并正确处理性别称呼。
