# 外呼查询字段

`GET /calls` 返回最近一批外呼记录。

`GET /calls/{call_id}` 返回某一通电话的详细状态。

## 业务系统优先看这些字段

```text
call_id
external_call_id
status
phase
failure_reason
hangup_cause
sip_status
elapsed_ms
talk_duration_ms
```

## 字段分类

```text
身份字段
  call_id、external_call_id、destination、context

路由字段
  endpoint、requested_endpoint、dialplan_extension、dialplan_context、caller_id_name、caller_id_number

状态字段
  status、phase、phase_label

时间字段
  created_at_ms、started_at_ms、ringing_at_ms、answered_at_ms、media_connected_at_ms、completed_at_ms

耗时字段
  elapsed_ms、originate_elapsed_ms、answer_latency_ms、ringing_ms、talk_duration_ms

FreeSWITCH 诊断字段
  freeswitch_reply、last_event_name、hangup_cause、sip_status、sip_reason

失败解释字段
  error、failure_reason、failure_label、failure_hint、sip_status_hint
```

## 正常结束

```text
status=completed
phase=completed
hangup_cause=NORMAL_CLEARING
failure_reason=null
```

含义：

```text
电话打通了
通话正常结束
不是失败
```

## 临时失败

```text
status=failed
phase=temporary_failure
hangup_cause=NORMAL_TEMPORARY_FAILURE
sip_status=503
failure_reason=NORMAL_TEMPORARY_FAILURE
```

含义：

```text
这通电话没有成功接通
失败原因是临时不可用
SIP 层返回了 503
```

常见原因：

```text
本地软电话 Contact 不可达
FreeSWITCH 到对端网络不通
SIP Trunk 临时不可用
运营商侧临时失败
NAT 或端口映射问题
```

相关笔记：[[04-channel-events|Channel 事件和外呼状态机]]
