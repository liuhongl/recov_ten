# 坐席页人工接管建议设计

## 背景

现有网关已经支持客户明确要求转人工后的 `waiting_agent -> claim -> bridge` 链路。现在需要新增一个更轻量的人工接管建议能力：当 AI 和客户通话过程中识别到客户说“我想投诉”时，先只在坐席页面提示“建议人工接管”，由坐席决定是否打断当前 AI 通话并接管。

第一版只处理固定语义：

```text
我想投诉
```

后续可以把更多投诉、升级、情绪或风险语义扩展到同一套建议机制中。

## 采用方案

采用“提示和接管分离”的方案。

```text
客户说“我想投诉”
-> 实时网关识别投诉意图
-> 当前通话记录新增 agent_takeover_suggestion
-> AI 继续正常通话
-> static/webrtc-agent-test.html 坐席页展示“建议人工接管”
-> 坐席点击“人工接管”
-> 后端再进入现有 handoff 流程
-> 坐席 claim 后由 FreeSWITCH bridge 客户通道和坐席通道
```

这个方案不复用 `waiting_agent` 作为提示状态。`waiting_agent` 仍然表示已经正式请求人工接管；而 `agent_takeover_suggestion` 只表示“建议坐席关注并可主动接管”。

## 运行态数据

在 `OutboundCallRecord` 上新增可序列化的接管建议状态，随 `/calls` 和 `/calls/{call_id}` 返回：

```json
{
  "agent_takeover_suggestion": {
    "state": "suggested",
    "reason": "complaint",
    "last_utterance": "我想投诉",
    "suggested_at_ms": 1710000000000,
    "updated_at_ms": 1710000000000,
    "can_takeover": true
  }
}
```

字段含义：

```text
state: suggested 表示当前有人工接管建议
reason: 第一版固定为 complaint
last_utterance: 触发建议的客户原始转写文本
suggested_at_ms: 首次触发时间
updated_at_ms: 最近更新时间
can_takeover: 当前通话仍可由坐席主动接管
```

第一版建议状态只放在 Python 内存运行态和接口返回中，不新增数据库表，也不修改 `call_record` 主表结构。

## 语义识别

第一版只做明确规则识别：

```text
客户 ASR 文本去掉空白和常见标点后，等于“我想投诉”。
```

示例：

```text
我想投诉 -> 命中
我想，投诉 -> 命中
我想投诉你们物业 -> 不命中，后续语义配置再扩展
我不是想投诉 -> 不命中
```

命中后只记录建议，不调用现有 `request_handoff`，不停止 AI 播放，不关闭实时会话，不延迟最终通话结果。

## 坐席页交互

提示只放在 `static/webrtc-agent-test.html`。

页面轮询 `/calls?status=active&limit=50` 时，同时筛选：

```text
call.agent_takeover_suggestion.state == "suggested"
call.agent_takeover_suggestion.can_takeover == true
```

页面新增“建议人工接管”列表，或在现有待接通话区域旁边展示建议卡片。建议卡片至少展示：

```text
Call ID
客户号码
触发原因：客户投诉
客户原话：我想投诉
按钮：人工接管
```

坐席点击“人工接管”后，前端按顺序执行：

```text
POST /calls/{call_id}/handoff
POST /calls/{call_id}/handoff/claim
```

第一步把建议升级为正式转人工请求，第二步复用现有坐席接管和 FreeSWITCH bridge 能力。

## 状态边界

普通 AI 通话不受影响。

如果同一通电话已经存在正式 `handoff`，页面应以 `handoff` 状态为准，不再展示建议接管按钮。

如果通话已经结束、失败、忙线或挂断，`can_takeover` 应为 `false`，页面不允许接管。

如果坐席点击接管时通话已经不可接管，后端按现有 `request_handoff` / `claim_handoff` 错误返回处理，页面展示错误日志。

## 测试计划

单元测试覆盖：

```text
1. 客户 ASR 文本“我想投诉”会写入 agent_takeover_suggestion。
2. 触发建议后不会设置 session.handoff_requested，不会调用 request_handoff。
3. 非“我想投诉”文本不会写入建议。
4. /calls 返回包含 agent_takeover_suggestion。
5. 通话进入终态后 can_takeover 为 false。
```

前端手工验证：

```text
1. 坐席页上线注册成功。
2. 客户说“我想投诉”后，坐席页出现“建议人工接管”提示。
3. AI 在提示出现后仍继续通话。
4. 坐席点击“人工接管”后，通话进入现有 waiting_agent / claim / bridge 流程。
5. 接管失败时页面日志展示后端错误。
```
