# HTTP 控制面

HTTP 控制面负责“呼叫控制”，不是通话音频本身。

## 发起外呼

```http
POST /calls
Content-Type: application/json
```

```json
{
  "destination": "1000",
  "external_call_id": "local-test-001",
  "caller_id_number": "9000",
  "context": {
    "scene": "local-outbound-test"
  }
}
```

这个请求表示：

```text
业务系统告诉网关：
  我要打一通电话
  被叫是 1000
  主叫显示 9000
  这通电话属于 local-outbound-test 场景
```

网关收到后会：

```text
1. 校验 JSON
2. 生成内部 call_id
3. 保存 external_call_id 和 context
4. 根据 destination 解析 endpoint
5. 通过 Event Socket 命令 FreeSWITCH originate
6. 等待 FreeSWITCH channel 事件更新状态
```

## 字段说明

```text
destination
  被叫号码。本地测试通常是 MicroSIP 分机 1000；真实线路通常是手机号或运营商要求的号码格式。

external_call_id
  业务系统自己的通话 ID，可选。用于把网关通话和业务记录关联起来。

caller_id_number
  主叫号码。本地默认 9000，避免 1000 呼叫 1000 的自呼叫歧义。

endpoint
  可选，覆盖默认呼叫目标。真实 SIP trunk 可能使用 sofia/gateway/<trunk-name>/{destination}。

context
  业务上下文 JSON。当前主要保存和透传，后续可注入 AI prompt。

originate_timeout_seconds
  外呼超时时间，默认 30 秒。
```

## 为什么不让业务系统直接调 FreeSWITCH

业务系统应该使用业务语义：

```text
POST /calls
GET /calls/{call_id}
POST /calls/{call_id}/hangup
```

而不是直接处理：

```text
FreeSWITCH originate
Event Socket 鉴权
SIP 状态码
Contact 解析
9199 拨号计划
媒体 WebSocket
```

这就是 [[01-system-overview|网关]] 存在的价值。
