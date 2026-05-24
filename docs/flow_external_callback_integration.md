# 外部系统流程回调接入说明

## 一、定位

催收流程引擎只负责节点编排状态。外呼、RPA、送达等外部系统负责自己的业务执行、业务表、产出和失败详情。

外部系统不要直接修改 Java 流程表。外部系统完成业务状态变化后，通过 RocketMQ callback 通知 Java 流程引擎。

## 二、核心字段

| 字段 | 含义 |
|---|---|
| `taskId` | Java 流程节点执行记录 ID，是回调关联键，外部系统必须原样回传。 |
| `businessId` | 外部系统自己的任务 ID，例如外呼任务 ID、RPA 任务 ID、送达任务 ID。 |
| `tenantId` | 租户编号。建议回传，Java 会校验与执行记录租户一致。 |
| `status` | 回调状态。支持 `ACCEPTED`、`PROGRESS`、`SUCCESS`、`FAILED`、`SKIPPED`。 |
| `message` | 面向流程追踪和前端展示的摘要信息。详细过程和产物仍以外部系统业务表为准。 |
| `timestamp` | 外部系统产生回调的毫秒时间戳。 |

## 三、Callback Topic

默认 topic：

```text
recov-flow-callback
```

以 Nacos 配置 `recov.flow.mq.callback-topic` 为准。

RocketMQ tag 建议直接使用 `status`：

```text
ACCEPTED / PROGRESS / SUCCESS / FAILED / SKIPPED
```

## 四、RocketMQ 接入参数

### 4.1 Java 侧当前约定

| 参数 | 当前值 / 配置项 | 说明 |
|---|---|---|
| Trigger Topic | `recov-flow-trigger` | Java 流程引擎下发节点任务。以 Nacos 配置 `recov.flow.mq.trigger-topic` 为准。 |
| Callback Topic | `recov-flow-callback` | 外部系统回调流程节点状态。以 Nacos 配置 `recov.flow.mq.callback-topic` 为准。 |
| Callback Consumer Group | `recov-flow-callback-group` | Java 流程引擎消费 callback 的消费者组。以 Nacos 配置 `recov.flow.mq.callback-consumer-group` 为准。 |
| Callback Tag | `ACCEPTED` / `PROGRESS` / `SUCCESS` / `FAILED` / `SKIPPED` | 建议 Python 发送 callback 时直接使用 `status` 作为 tag。 |
| Trigger Tag | 节点 `nodeCode`，外呼为 `ai_call` | Python 外呼系统如需消费 Java trigger，应订阅 `recov-flow-trigger:ai_call`。 |

Java 侧 RocketMQ nameserver 由 Spring RocketMQ 配置提供：

```yaml
rocketmq:
  name-server: ${ROCKETMQ_NAME_SERVER}
  producer:
    group: ${ROCKETMQ_PRODUCER_GROUP}
```

### 4.2 已知环境地址

| 环境 | nameserver | 说明 |
|---|---|---|
| 本地 Docker 示例 | `127.0.0.1:9876` 或 `localhost:9876` | 来自仓库 `script/docker/docker-compose.yml` 和 `ruoyi-example/ruoyi-test-mq` 示例，仅用于开发参考，不能作为验收环境。 |
| 测试环境 | `118.89.137.44:9876` | 已确认真实环境开放 RocketMQ nameserver `9876` 端口。HTTP 入口为 `http://118.89.137.44/`。 |
| 生产环境 | `118.89.137.44:9876` | 已确认真实环境开放 RocketMQ nameserver `9876` 端口。不要把 ACL 密钥写入代码仓库。 |

说明：

1. `http://118.89.137.44/` 是已确认的 MQ HTTP 入口地址。
2. 当前 Java 使用 Spring RocketMQ native client，`rocketmq.name-server` 应配置为 `118.89.137.44:9876`，不要把 HTTP URL 直接填入 native client。
3. 自测和联调必须使用测试环境 `118.89.137.44:9876`，不能用本地 RocketMQ 或本地 Java 服务替代。
4. Topic 需要提前创建并授权；不要依赖线上自动创建 Topic。

### 4.3 Python 侧建议配置

Python 外呼项目建议通过配置文件或环境变量控制 callback 开关和 RocketMQ 参数，默认关闭真实发送。

```toml
[flow_callback]
enabled = false

[rocketmq]
enabled = false
endpoint = "http://118.89.137.44/"
name_server = "118.89.137.44:9876"
producer_group = "recov-ten-gateway"
callback_topic = "recov-flow-callback"

[rocketmq.acl]
enabled = false
access_key_env = "ROCKETMQ_ACCESS_KEY"
secret_key_env = "ROCKETMQ_SECRET_KEY"
security_token_env = "ROCKETMQ_SECURITY_TOKEN"
```

参数说明：

| 参数 | 必填 | 说明 |
|---|---|---|
| `flow_callback.enabled` | 是 | 是否真实发送流程 callback。测试环境联调前再打开。 |
| `rocketmq.enabled` | 是 | RocketMQ 总开关。`false` 时 Python 不初始化 MQ producer、不连接 MQ、不发送 callback；`true` 时才允许真实发送。 |
| `rocketmq.endpoint` | 是 | 已确认 MQ 入口地址，当前所有环境为 `http://118.89.137.44/`。 |
| `rocketmq.name_server` | 使用 native client 时必填 | RocketMQ native nameserver 地址，真实环境为 `118.89.137.44:9876`。不能把 HTTP URL 直接填入 native client。 |
| `rocketmq.producer_group` | 是 | Python callback 生产者组。当前预留为 `recov-ten-gateway`，如运维另定则以运维配置为准。 |
| `rocketmq.callback_topic` | 是 | 默认 `recov-flow-callback`。如 Java Nacos 改了 `recov.flow.mq.callback-topic`，Python 必须同步修改。 |
| `rocketmq.acl.enabled` | 是 | 是否启用 ACL。当前仓库没有线上 ACL 事实配置。 |
| `rocketmq.acl.access_key_env` | 启用 ACL 时必填 | AccessKey 所在环境变量名，不能在仓库中写明文密钥。 |
| `rocketmq.acl.secret_key_env` | 启用 ACL 时必填 | SecretKey 所在环境变量名，不能在仓库中写明文密钥。 |
| `rocketmq.acl.security_token_env` | 按环境要求 | 临时 token 所在环境变量名；如果环境不需要 token，可为空变量。 |

Python 客户端选型需要单独确认。RocketMQ Python 客户端可能依赖 native 库，必须先确认线上环境可安装、可运维、可观测，再写真实发送代码。

开关语义：

1. `rocketmq.enabled=false`：MQ 能力整体关闭，Python 不应创建 RocketMQ producer，也不应尝试连接 `118.89.137.44:9876`。
2. `flow_callback.enabled=false`：流程 callback 真实发送关闭。可保留本地日志、测试桩或待发送记录，但不能向 `recov-flow-callback` 发送消息。
3. 只有 `rocketmq.enabled=true` 且 `flow_callback.enabled=true` 时，Python 才能真实发送流程 callback。
4. 关闭 MQ 时，如果业务逻辑仍执行外呼，应明确记录“流程 callback 未发送”，避免误判 Java 流程未推进是 Java 侧问题。

### 4.4 首次联调建议

首次真实 callback 联调必须在测试环境打开，不能使用本地环境替代：

```toml
[flow_callback]
enabled = true

[rocketmq]
enabled = true
```

验收链路：

```text
Java 初始化流程节点和 call_record
-> Java 下发 ai_call trigger，传 tenantId/taskId/callId/debtId/identityName
-> Python 完成外呼或使用模拟结果
-> Python 发送 SUCCESS / FAILED callback 到 recov-flow-callback
-> Java 消费 callback
-> 节点状态推进正确
-> 重复 callback 不重复推进流程
```

### 4.5 Python 侧真实环境全链路自测要求

Python 外呼侧在交付 Java / 业务联调前，必须完成自己模块的测试环境全链路自测。这里的“测试环境”指使用 `118.89.137.44:9876`、测试环境 callback topic、测试环境 Java 流程消费侧和测试环境数据库记录；不能只验证本地单元测试、日志输出、`LoggingFlowCallbackWriter`、本地 RocketMQ 或本地 Java 服务。

自测前置条件：

1. Java 侧流程服务已连接真实 MQ，并正在消费 `recov-flow-callback`。
2. Python 侧已配置真实 MQ 入口 `http://118.89.137.44/`，native client 的 `name_server` 配置为 `118.89.137.44:9876`。
3. `rocketmq.enabled = true` 和 `flow_callback.enabled = true` 只在自测窗口开启；测试完成后按环境策略关闭或保留。
4. ACL 如启用，`access_key`、`secret_key`、`security_token` 必须通过环境变量、配置中心或密钥系统注入，不得写入代码仓库。
5. 使用专用测试租户、测试债务、测试流程实例和测试手机号。未获得确认前，不要对真实债务人发起外呼。
6. `taskId` 必须来自 Java 已创建的流程节点执行记录，不能由 Python 自行伪造。
7. `call_record` 必须由 Java 或 Java 侧外呼适配器初始化；Python 找不到 `call_record` 时不要自行插入。
8. 自测报告必须能证明使用的是测试环境，不接受本地环境截图、日志或本地 MQ message id 作为通过依据。

Python 侧最小自测场景：

| 场景 | Python 行为 | 预期 Java 结果 |
|---|---|---|
| 成功外呼 | 更新 `call_record.status='4'`，写入 `transcript`，发送 `SUCCESS` | 节点成功，流程进入下一节点或完成。 |
| 外呼失败 | 更新 `call_record.status='2'`，发送 `FAILED` | 节点失败，按节点失败策略处理。 |
| 未接听 / 忙线 / 拒接 / 无有效内容 | 更新 `call_record.status='3'` 或失败终态，发送 `FAILED` | 节点失败，按节点失败策略处理。 |
| 业务规则主动跳过外呼 | 不发起外呼，或记录跳过原因，发送 `SKIPPED` | 节点跳过，按节点跳过策略处理。 |
| 过程进度 | 发送 `ACCEPTED` 和可选 `PROGRESS` | 节点状态和轨迹可展示，但不推进流程。 |
| 重复终态 | 对同一 `taskId` 重复发送一次相同终态 | Java 只推进一次，重复消息被忽略。 |

自测必须留存的证据：

1. Python 实际生效配置，隐藏密钥后记录 `endpoint`、`name_server`、`producer_group`、`callback_topic`、ACL 是否开启。
2. Java 下发给 Python 的入参：`tenantId`、`taskId`、`callId`、`debtId`、`identityName`。
3. Python callback 原始报文、发送时间、发送结果和 MQ message id。
4. `call_record` 自测前后状态、`started_at`、`finished_at`、`transcript`。
5. Java 侧消费 callback 的日志或 outbox / trace 证据。
6. `recov_node_execution_record` 的 `exec_status`、`business_id`、`result_message`。
7. `recov_flow_instance` 的当前节点、流程状态和是否推进。
8. 重复终态场景的幂等证据。

通过标准：

1. Python 真实发送到 `recov-flow-callback`，不是只打印日志。
2. 测试环境 Java 能消费 Python 发送的 callback。
3. `tenantId` 校验通过，没有串租户。
4. `businessId` 能关联到 Python 外呼任务或 `callId`。
5. 成功、失败、业务主动跳过三类终态都能按预期影响流程。
6. 重复终态不会重复推进流程。
7. Python 不直接修改 Java 流程表。
8. Python 不写 `analysis_status`、`analysis_result`、`analysis_error`。

## 五、消息格式

```json
{
  "tenantId": "000000",
  "taskId": "2050000000000100001",
  "businessId": "rpa-202605230001",
  "status": "SUCCESS",
  "message": "RPA立案材料提交成功",
  "timestamp": 1770000000000
}
```

## 六、状态语义

| status | 是否终态 | 作用 |
|---|---|---|
| `ACCEPTED` | 否 | 外部系统已受理任务，Java 节点进入已接收状态。 |
| `PROGRESS` | 否 | 更新节点进度信息，不推进流程。可发送多次。 |
| `SUCCESS` | 是 | 节点成功，流程推进到下一节点或完成。 |
| `FAILED` | 是 | 节点失败，按节点失败策略处理。 |
| `SKIPPED` | 是 | 节点跳过，按节点跳过策略处理。 |

终态只能是 `SUCCESS`、`FAILED`、`SKIPPED` 三选一。

## 七、幂等与乱序

Java 流程引擎已做回调幂等：

1. `ACCEPTED` 只允许从 `TRIGGERED` 更新。
2. `PROGRESS` 只更新 `TRIGGERED` 或 `ACCEPTED` 状态的节点。
3. `SUCCESS`、`FAILED`、`SKIPPED` 只允许从非终态更新。
4. 已终态节点收到重复终态或迟到 `PROGRESS` 会被忽略，不会重复推进流程。
5. 回调中的 `tenantId` 如果和节点执行记录租户不一致，会被拒绝。

## 八、超时规则

流程实例进入等待回调后，如果外部系统一直不返回终态，Java 调度器会按节点配置的 `timeoutMinutes` 或默认超时时间处理。

当前默认超时后标记节点失败，流程进入 `NODE_FAILED`，等待人工处理或重试。

## 九、外部系统开发规范

外部系统推荐流程：

1. 接收 Java 下发任务，保存自己的业务表。
2. 原样保存 Java 传入的 `taskId`。
3. 自己执行业务逻辑，并在自己的业务表记录过程、产物、截图、失败详情。
4. 受理成功后发送 `ACCEPTED` 或由 Java 在受理成功时写入 `ACCEPTED`。
5. 执行过程中可选发送 `PROGRESS`。
6. 完成后发送且只发送一个终态：`SUCCESS`、`FAILED` 或 `SKIPPED`。
7. 不直接写 Java 流程表。

## 十、Python/RPA 示例

Java 调 RPA 的最小请求契约：

```json
{
  "tenantId": "000000",
  "taskId": "2050000000000100001",
  "debtId": "2050000000000200001"
}
```

RPA 自己根据 `debtId` 查询立案材料并维护自己的业务表。

RPA 执行成功后发送：

```json
{
  "tenantId": "000000",
  "taskId": "2050000000000100001",
  "businessId": "rpa-202605230001",
  "status": "SUCCESS",
  "message": "RPA立案材料提交成功",
  "timestamp": 1770000000000
}
```

RPA 执行失败后发送：

```json
{
  "tenantId": "000000",
  "taskId": "2050000000000100001",
  "businessId": "rpa-202605230001",
  "status": "FAILED",
  "message": "RPA立案材料提交失败：法院系统返回材料不完整",
  "timestamp": 1770000000000
}
```

详细失败原因、截图和法院返回原文应保存在 RPA 自己的业务表中。

## 十一、AI 外呼节点交互格式

### 11.1 交互边界

AI 外呼节点仍遵循本文档的通用边界：

1. Java 流程引擎负责节点编排、节点执行记录、流程推进和超时处理。
2. 外呼系统负责实际拨打、外呼任务表、供应商调用、通话转写、失败详情和产物留存。
3. 外呼系统不要直接修改 Java 流程表。
4. 外呼系统完成业务状态变化后，通过 `recov-flow-callback` 回调 Java 流程引擎。

AI 外呼还额外涉及 `call_record`：

1. `call_record` 是外呼通话事实表。
2. 当前最小落地约定中，`callId` 使用 Java 下发的 `taskId`，即 `call_record.id = taskId`。
3. `businessId` 建议回传 `callId`，方便流程追踪和外呼通话记录互查。
4. 如果后续需要独立的外呼任务 ID，应新增外呼任务表，并把该任务 ID 作为 `businessId` 回传；`callId` 仍用于定位 `call_record`。
5. 真实 Python 外呼接入时，推荐由 Java 或 Java 侧外呼适配器先初始化 `call_record`，Python 只更新通话状态和转写结果。Python 找不到 `call_record` 时不要自行插入，应记录失败并回调 `FAILED`。

### 11.2 Java 下发外呼任务

Topic：

```text
recov-flow-trigger
```

Tag：

```text
ai_call
```

外呼系统需要订阅 `recov-flow-trigger:ai_call`。

当前 Java 实现会把 `TriggerMessage` 完整序列化后写入 outbox，再投递到 MQ。也就是说，Python 实际收到的消息体当前包含 `params` 和 `flowContext`：

```json
{
  "schemaVersion": "1.0",
  "traceId": "2050000000000100001",
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "instanceId": 2050000000000001001,
  "stepId": "ai_call_1",
  "stepIndex": 0,
  "nodeCode": "ai_call",
  "identityName": "项目员工",
  "debtId": "2050000000000200001",
  "debtRecordId": 2050000000000200001,
  "timestamp": 1770000000000,
  "params": {
    "timeoutMinutes": 30
  },
  "flowContext": {
    "schemaVersion": "1.0",
    "debt": {
      "debtRecordId": "2050000000000200001",
      "personaId": "2050000000000300001",
      "debtorName": "张三"
    }
  }
}
```

说明：`params` 和 `flowContext` 属于 Java 流程编排上下文。为了和当前 Java 实现对齐，消息体里会保留这两个字段；但 Python 外呼侧不应依赖它们，业务执行只按下面的必需字段处理。

字段说明：

| 字段 | Java 当前下发 | Python 侧依赖 | 说明 |
|---|---|---|---|
| `schemaVersion` | 是 | 建议校验 | 消息版本，当前为 `1.0`。 |
| `traceId` | 是 | 否 | 链路追踪 ID，当前与 `taskId` 一致，用于日志排障。 |
| `tenantId` | 是 | 是 | 租户编号。外呼系统用于业务隔离、日志和回调校验。 |
| `taskId` | 是 | 是 | Java 流程节点执行记录 ID，必须原样回传。当前也作为 `callId` 使用。 |
| `instanceId` | 是 | 否 | 流程实例 ID，用于排查问题。 |
| `stepId` | 是 | 否 | 流程模板步骤 ID，用于排查问题。 |
| `stepIndex` | 是 | 否 | 当前步骤序号，用于排查问题。 |
| `nodeCode` | 是 | 建议校验 | 节点编码，外呼固定为 `ai_call`。 |
| `identityName` | 是 | 是 | 外呼身份，例如 `项目员工`、`企业客服`、`律师`。 |
| `debtId` | 是 | 是 | 债务记录 ID，按字符串处理。 |
| `debtRecordId` | 是 | 否 | 债务记录 ID，Java long 形态。外部系统优先使用 `debtId` 字符串。 |
| `timestamp` | 是 | 否 | Java 生成 trigger 的毫秒时间戳，用于日志排障。 |
| `params` | 是，可能为空 | 否 | Java 流程节点参数，当前会随完整 `TriggerMessage` 下发。Python 外呼侧不要依赖。 |
| `flowContext` | 是，可能为空 | 否 | Java 流程实例上下文快照，当前会随完整 `TriggerMessage` 下发。Python 外呼侧不要依赖。 |

外呼系统内部执行时可转换成最小入参：

```json
{
  "callId": "2050000000000100001",
  "taskId": "2050000000000100001",
  "debtId": "2050000000000200001",
  "tenantId": "100001",
  "identityName": "项目员工"
}
```

说明：

1. `callId` 当前取 `taskId`。
2. `taskId` 用于流程回调，不能改名、不能重新生成。
3. `debtId` 用于查询债务、债务人电话、画像、话术、身份音色等外呼业务数据；当前 Python 网关按 `debt_record.debtor_phone` 解析被叫号码。
4. `tenantId` 不建议作为更新 `call_record` 的 where 条件，但必须用于日志、隔离校验和回调。

### 11.3 call_record 状态与流程回调映射

外呼系统应先确认并维护 `call_record`，再发送流程 callback。终态 callback 必须在本地业务状态提交后发送。

| 外呼业务动作 | `call_record.status` | 流程 callback `status` | 说明 |
|---|---|---|---|
| 已受理任务 | `0` 或 `1` | `ACCEPTED` | 可选但推荐。表示外呼系统已收到任务。 |
| 开始拨打 | `1` | `PROGRESS` | 可选。用于前端轨迹展示，不推进流程。 |
| 转写完成 | `4` | `SUCCESS` | 表示本次外呼节点成功，可推进后续流程。 |
| 供应商失败 / 系统异常 / 无法执行 | `2` | `FAILED` | 表示节点失败，按流程节点失败策略处理。 |
| 未接听 / 忙线 / 拒接 / 无有效通话内容 | `3` 或失败终态 | `FAILED` | 表示本次外呼未产生有效通话产物，按流程节点失败策略处理。 |
| 业务规则主动跳过外呼 | 按业务表记录跳过原因 | `SKIPPED` | 例如号码为空、免催、黑名单、非外呼时间窗等明确跳过场景。 |

注意：

1. `call_record.status = '4'` 只表示转写完成，不表示语义分析成功。
2. 外呼系统不要写 `analysis_status`、`analysis_result`、`analysis_error`。
3. `FAILED` 和 `SKIPPED` 的选择会影响流程策略：系统异常、供应商异常、用户未接听、关机、无人应答、忙线、拒接默认用 `FAILED`；只有业务规则明确决定不执行外呼时才用 `SKIPPED`。

### 11.4 外呼回调消息

Topic：

```text
recov-flow-callback
```

Tag 建议使用 `status`：

```text
ACCEPTED / PROGRESS / SUCCESS / FAILED / SKIPPED
```

受理成功：

```json
{
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "businessId": "2050000000000100001",
  "status": "ACCEPTED",
  "message": "AI外呼任务已受理",
  "timestamp": 1770000001000
}
```

拨打中：

```json
{
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "businessId": "2050000000000100001",
  "status": "PROGRESS",
  "message": "AI外呼已开始拨打",
  "timestamp": 1770000002000
}
```

转写完成：

```json
{
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "businessId": "2050000000000100001",
  "status": "SUCCESS",
  "message": "AI外呼完成，通话转写已生成",
  "timestamp": 1770000010000
}
```

外呼失败：

```json
{
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "businessId": "2050000000000100001",
  "status": "FAILED",
  "message": "AI外呼失败：供应商返回号码不可拨打",
  "timestamp": 1770000010000
}
```

未接听：

```json
{
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "businessId": "2050000000000100001",
  "status": "FAILED",
  "message": "AI外呼未接听，未产生有效通话内容",
  "timestamp": 1770000010000
}
```

业务规则主动跳过：

```json
{
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "businessId": "2050000000000100001",
  "status": "SKIPPED",
  "message": "AI外呼跳过：号码为空或命中免催规则",
  "timestamp": 1770000010000
}
```

### 11.5 transcript 格式

外呼成功时，外呼系统应在发送 `SUCCESS` 前写入 `call_record.transcript`。

`transcript` 字段类型为 text，内容必须是 JSON 字符串，推荐结构：

```json
{
  "version": "1.0",
  "provider": "python-ai-call",
  "callId": "2050000000000100001",
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

### 11.6 幂等要求

1. 外呼系统以 `taskId` / `callId` 作为幂等键。
2. 同一个 `taskId` 只能产生一个终态 callback。
3. 重复收到同一个 trigger 时，不要重复创建供应商外呼任务。
4. 如果 `call_record` 已是 `2`、`3`、`4`，不要覆盖终态。
5. Java 流程引擎会忽略已终态节点的重复终态或迟到 `PROGRESS`，但外呼系统仍应避免重复发送。
