# Python AI 外呼流程 HTTP 接入实现指南

## 一、目标

Python AI 外呼侧通过 HTTP 和 Java 催收流程引擎集成：

```text
Java 流程引擎
-> Java 消费 ai_call trigger
-> Java 调用 Python /calls
-> Python 创建或复用外呼任务
-> Python 更新 call_record
-> Python 回调 Java /system/recov/flow/external/callback
-> Java 推进流程节点
```

Python 不需要直接接入 RocketMQ。RocketMQ 由 Java 内部使用，避免 Python RocketMQ 客户端 native 依赖、部署兼容和运维风险。

## 二、Java 侧配置前置

测试环境 Java 需要开启：

```yaml
recov:
  flow:
    ai-call-http:
      enabled: true
      base-url: http://python-ai-call.test
      calls-path: /calls
      retry-delay-seconds: 60
    external-callback:
      enabled: true
      auth-enabled: true
      clients:
        python-ai-call:
          secret: ${FLOW_CALLBACK_HMAC_SECRET_AI_CALL}
```

网关白名单需要包含：

```text
/system/recov/flow/external/callback
```

Python 侧必须使用测试环境服务和测试环境数据库自测，不能用本地 Java、本地 RocketMQ 或本地数据库作为验收依据。

## 三、Python 需要提供 `/calls`

Java 会调用：

```text
POST /calls
Content-Type: application/json
```

请求体是完整流程 trigger，并补充 `callId`：

```json
{
  "schemaVersion": "1.0",
  "traceId": "2050000000000100001",
  "tenantId": "100001",
  "taskId": "2050000000000100001",
  "callId": "2050000000000100001",
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
    "schemaVersion": "1.0"
  }
}
```

Python 依赖字段：

| 字段 | 说明 |
|---|---|
| `tenantId` | 租户编号，用于隔离、日志和回调。 |
| `taskId` | Java 流程节点执行记录 ID，必须原样回传。 |
| `callId` | 当前等于 `taskId`，对应 `call_record.id`。 |
| `debtId` | 债务记录 ID，按字符串处理。 |
| `identityName` | 外呼身份。 |
| `destination` | 可选调试字段。正常流程下 Java 不需要传，Python 根据 `debtId` 查询 `debt_record.debtor_phone` 作为被叫号码。 |

Python 不应依赖 `params` 和 `flowContext` 做核心业务判断；它们是 Java 流程上下文，保留只是为了当前实现兼容和排查。

当前 Python 网关内部还会生成 FreeSWITCH 通话 UUID，用于 `origination_uuid` 和媒体链路关联。这个内部 UUID 可以和 Java `callId` 不同；流程回调、`call_record` 更新和幂等判断仍以 Java `callId/taskId` 为准。

号码解析规则：

1. `/calls` 如果显式传入 `destination`，Python 优先使用该值，主要用于本地调试和人工测试。
2. 正常 Java 流程触发时不传 `destination`，Python 使用 `debtId` 查询：

```sql
select debtor_phone
from debt_record
where id = :debt_id
limit 1;
```

3. 查不到债务记录、`debtor_phone` 为空或拨号目标包含非法字符时，Python 不发起外呼，返回业务错误。
4. PostgreSQL 不可用或查询超时时，Python 返回外部资源不可用错误，Java 应按 `/calls` 5xx/超时策略重试。

`/calls` 建议响应：

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": {
    "accepted": true,
    "businessId": "2050000000000100001",
    "message": "AI外呼任务已受理"
  }
}
```

Java 也兼容无 `R` 包装的响应：

```json
{
  "accepted": true,
  "businessId": "2050000000000100001",
  "message": "AI外呼任务已受理"
}
```

响应语义：

| 响应 | Java 行为 |
|---|---|
| HTTP 2xx 且 `accepted=true` | Java 将流程节点置为 `ACCEPTED`。 |
| HTTP 2xx 且 `accepted=false` | Java 将 `call_record` 标记失败，并推进流程 `FAILED`。 |
| HTTP 4xx | Java 认为业务拒绝，推进流程 `FAILED`。 |
| HTTP 5xx / 超时 / 连接失败 | Java 认为外部资源暂时不可用，不置失败，延迟重投 trigger。 |

`/calls` 幂等要求：

1. 以 `taskId` 或 `callId` 作为幂等键。
2. 重复收到同一个 `taskId`，不能重复创建供应商外呼任务。
3. 如果已有外呼任务，直接返回 `accepted=true` 和原 `businessId`。
4. Python 找不到 Java 已初始化的 `call_record` 时，不要自行插入，应返回业务失败或记录错误后 callback `FAILED`。

## 四、Python 更新 `call_record`

当前约定：

```text
call_record.id = callId = taskId
```

状态映射：

| Python 外呼结果 | `call_record.status` | callback `status` |
|---|---|---|
| 已受理 / 等待拨打 | `0` 或 `1` | `ACCEPTED`，可由 Java 在 `/calls` 成功后自动写入流程节点。 |
| 拨打中 | `1` | `PROGRESS`，可选。 |
| 转写完成 | `4` | `SUCCESS`。 |
| 供应商失败 / 系统异常 / 参数不可执行 | `2` | `FAILED`。 |
| 未接听 / 关机 / 忙线 / 拒接 / 无有效通话内容 | `3` 或失败终态 | `FAILED`。 |
| 业务规则主动跳过外呼 | 按业务表记录跳过原因 | `SKIPPED`。 |

注意：

1. `call_record.status='4'` 只表示转写完成，不表示语义分析完成。
2. Python 不要写 `analysis_status`、`analysis_result`、`analysis_error`。
3. Python 不要直接修改 Java 流程表，例如 `recov_node_execution_record`、`recov_flow_instance`。
4. 终态 callback 必须在本地 `call_record` 状态和转写结果提交成功后发送。

## 五、Python 回调 Java

回调地址：

```text
POST /system/recov/flow/external/callback
Content-Type: application/json
```

成功回调 body：

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

支持状态：

| status | 是否终态 | 说明 |
|---|---|---|
| `ACCEPTED` | 否 | 外部系统已受理任务。`/calls` 返回 `accepted=true` 后 Java 已自动处理，一般不用再发。 |
| `PROGRESS` | 否 | 更新节点进度，不推进流程。 |
| `SUCCESS` | 是 | 节点成功，流程进入下一节点或完成。 |
| `FAILED` | 是 | 节点失败，按节点失败策略处理。 |
| `SKIPPED` | 是 | 节点跳过，按节点跳过策略处理。 |

终态只能发送一个：`SUCCESS`、`FAILED`、`SKIPPED` 三选一。

## 六、HMAC 签名

Java callback 开启 HMAC 后，Python 每次回调必须带请求头：

```text
X-LC-Client-Id: python-ai-call
X-LC-Timestamp: 1770000001
X-LC-Nonce: 9f3c4f65b1f34a0fa9c947a00c3d1df1
X-LC-Signature: base64(hmac_sha256(secret, canonical))
X-LC-Signature-Path: /system/recov/flow/external/callback
```

签名原文：

```text
METHOD + "\n"
+ PATH + "\n"
+ TIMESTAMP + "\n"
+ NONCE + "\n"
+ SHA256_HEX(RAW_BODY)
```

字段说明：

| 字段 | 说明 |
|---|---|
| `METHOD` | 固定 `POST`。 |
| `PATH` | 使用 Python 实际请求的网关路径 `/system/recov/flow/external/callback`。 |
| `TIMESTAMP` | 秒级 Unix 时间戳。Java 默认允许 300 秒偏移。 |
| `NONCE` | 每次请求唯一，Java 会做防重放校验。 |
| `RAW_BODY` | 实际发送的 JSON 字符串，签名后不能再重新格式化。 |

Python 示例：

```python
import base64
import hashlib
import hmac
import json
import time
import uuid


def build_callback_headers(body: dict, secret: str) -> tuple[dict, str]:
    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    path = "/system/recov/flow/external/callback"
    body_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    canonical = "\n".join(["POST", path, timestamp, nonce, body_hash])
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-LC-Client-Id": "python-ai-call",
        "X-LC-Timestamp": timestamp,
        "X-LC-Nonce": nonce,
        "X-LC-Signature": signature,
        "X-LC-Signature-Path": path,
    }
    return headers, raw_body
```

## 七、callback 发送建议

Python 推荐实现一个 callback writer，统一处理重试和日志：

```python
import requests


def send_flow_callback(base_url: str, secret: str, body: dict) -> None:
    headers, raw_body = build_callback_headers(body, secret)
    url = base_url.rstrip("/") + "/system/recov/flow/external/callback"
    response = requests.post(url, data=raw_body.encode("utf-8"), headers=headers, timeout=10)
    if response.status_code >= 500:
        raise RuntimeError(f"flow callback temporary failed: {response.status_code} {response.text}")
    if response.status_code >= 400:
        raise RuntimeError(f"flow callback rejected: {response.status_code} {response.text}")
    payload = response.json()
    if payload.get("code") != 200:
        raise RuntimeError(f"flow callback business failed: {payload}")
```

重试策略：

1. HTTP 5xx、超时、网络异常：可重试，建议指数退避。
2. HTTP 400：不要盲目重试，优先检查签名、字段、租户、状态。
3. HTTP 404：说明 Java 未开启 callback 或网关未放行。
4. 同一 `taskId` 的终态 callback 重复发送不会重复推进 Java 流程，但 Python 仍应避免重复发送。

## 八、最小实现流程

`/calls` 收到请求后：

1. 校验 `nodeCode == "ai_call"`。
2. 校验 `tenantId`、`taskId`、`callId`、`debtId`、`identityName` 非空。
3. 如果请求没有 `destination`，用 `debtId` 查询 `debt_record.debtor_phone`。
4. 以 `callId` 查询 `call_record`。
5. 如果外呼任务已存在，返回原 `businessId`。
6. 创建 Python 自己的外呼任务或调用供应商。
7. 返回 `accepted=true`。

外呼结束后：

1. 成功：先写 `call_record.status='4'` 和 `transcript`，再 callback `SUCCESS`。
2. 失败：先写 `call_record.status='2'`，再 callback `FAILED`。
3. 未接听、忙线、拒接、无有效通话内容：先写 `call_record.status='3'` 或失败终态，再 callback `FAILED`。
4. 业务规则主动跳过外呼：记录跳过原因，再 callback `SKIPPED`。

## 九、自测清单

Python 侧交付前必须在测试环境完成：

| 场景 | 验证点 |
|---|---|
| `/calls` 正常受理 | Python 使用 `debtId` 查到 `debt_record.debtor_phone` 并发起外呼；Java 节点进入 `ACCEPTED`，`call_record` 初始记录存在。 |
| 债务手机号为空 | Python 不发起外呼，Java 节点按 `FAILED` 处理。 |
| 成功外呼 | `call_record.status='4'`，`transcript` 有 JSON，Java 节点 `SUCCESS`。 |
| 外呼失败 | `call_record.status='2'`，Java 节点 `FAILED`。 |
| 未接听 / 忙线 / 拒接 / 无有效通话内容 | `call_record.status='3'` 或失败终态，Java 节点 `FAILED`。 |
| 业务规则主动跳过 | 记录跳过原因，Java 节点 `SKIPPED`。 |
| 过程进度 | callback `PROGRESS` 后流程不推进，只更新进度信息。 |
| 重复终态 | 同一 `taskId` 重复发送 `SUCCESS`，Java 只推进一次。 |
| 签名错误 | Java 返回 400，不推进流程。 |
| nonce 重放 | 第二次同 nonce 请求返回 400。 |
| 租户不一致 | Java 返回 400 或拒绝推进。 |

必须留存证据：

1. Java 调 Python `/calls` 的入参。
2. Python 生成的 callback 原始 body 和 headers，隐藏密钥。
3. callback HTTP 响应状态和响应体。
4. `call_record` 前后状态。
5. `recov_node_execution_record.exec_status`、`business_id`、`result_message`。
6. `recov_flow_instance` 当前状态和是否推进。

## 十、RPA 复用方式

HTTP callback 是通用流程节点能力，不绑定 AI 外呼。RPA 后续可以使用同一个接口，只需要换独立客户端：

```yaml
recov:
  flow:
    external-callback:
      clients:
        rpa-filing:
          secret: ${FLOW_CALLBACK_HMAC_SECRET_RPA}
```

RPA 请求头使用：

```text
X-LC-Client-Id: rpa-filing
```

RPA body 仍使用通用 callback 格式，`businessId` 填 RPA 自己的任务 ID。
