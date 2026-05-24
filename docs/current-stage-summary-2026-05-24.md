# 2026-05-24 当前阶段总结

## 当前结论

当前阶段已经从本地软电话验证推进到公网真实线路预验证。代码侧、数据库更新链路和线上部署链路已经基本打通；真实电话线路仍卡在供应商侧 `508 / Q.850 cause=31`。

## 已完成

### 本地软电话链路

- Linphone `1000` 已能注册到本地 FreeSWITCH。
- `/outbound-test` 发起本地 `1000` 外呼可接通。
- 接通后能进入实时媒体网关和豆包 S2S。
- 挂机后 `call_record.status=4`，`transcript.turns` 正常写入。

### 数据库更新链路

Python 网关当前遵循以下边界：

```text
Java / 业务系统：初始化 public.call_record，生成 callId。
Python 网关：只根据 callId 更新 status / started_at / finished_at / transcript。
```

状态约定：

```text
0：初始化
1：已开始
2：失败
3：未接听
4：完成且 transcript 已写入
```

transcript 约定：

```json
{"turns":[{"role":"assistant","text":"..."},{"role":"user","text":"..."}]}
```

时间字段已改为数据库 `current_timestamp`，跟随数据库会话时区，避免 Python UTC-naive 写入导致少 8 小时。

### 线上部署

当前版本已部署到：

```text
111.229.146.182:/opt/recov_ten
```

线上状态：

```text
recov-ten-gateway.service：active
GET /health：ok
GET /ready：ready
sip-provider gateway：NOREG / UP
Ext-SIP-IP：111.229.146.182
Ext-RTP-IP：111.229.146.182
Contact 端口：5080
```

线上备份：

```text
/root/recov_ten_backup_20260524-161952-pre-current-version
/root/recov_ten_static_backup_20260524-164412
```

### 页面默认线路

`/outbound-test` 已调整：

- 默认场景从沙箱改为 `sip-provider 真实线路：手动号码`。
- 填写 `destination` 时自动生成真实线路 endpoint：

```text
sofia/gateway/sip-provider/{destination}
```

沙箱和本地软电话入口仍保留，可以手动选择。

## 真实线路样本

### 成功历史样本

2026-05-12，`111.229.146.182` 曾完成真实线路接通验证：

```text
主叫：037123124845
被叫：185****8743
SIP proxy：47.94.86.132:5089
Codec：PCMA/8000
ptime：20
最终阶段：media_connected
电话侧确认：可通话
```

### 当前失败样本

2026-05-24 当前版本真实线路测试：

```text
主叫：037123124845
被叫：185****8743
Python callId：7464233794932996429
FreeSWITCH call_id：7059405b9fcd4db59e760654481b2345
endpoint：sofia/gateway/sip-provider/185****8743
SIP 状态：508
Q.850 reason：31
hangup_cause：NORMAL_UNSPECIFIED
call_record.status：2
transcript：空
```

客观判断：

```text
我方已发出真实 INVITE，供应商 SBC 返回 183，SDP/codec 匹配成功；
但在 200 OK 前由供应商或上游返回 508 / cause=31。
```

这不是数据库问题，不是 Python 网关没有启动，也不是页面误走沙箱。

## 当前工作区分类

应沉淀的改动：

```text
.gitignore
app/call_control.py
app/main.py
app/postgres.py
app/realtime_phone_gateway.py
static/outbound-test.html
tests/test_call_control.py
tests/test_health_server.py
tests/test_postgres.py
tests/test_realtime_phone_gateway.py
docs/ai_call_python_db_operation_guide.md
docs/deploy-111229146182-current-version-checklist.md
docs/sip-provider-profile.md
docs/current-stage-summary-2026-05-24.md
```

本地环境改动，不应直接覆盖线上：

```text
freeswitch-local/conf/vars.xml
```

原因：该文件当前包含本机局域网 IP，用于本地 Linphone 软电话注册。线上应保持公网 `111.229.146.182`。

暂不纳入本次提交范围、需要后续单独确认的历史设计材料：

```text
docs/realtime-call-result-payload-comparison.md
docs/realtime-call-result-postgres-confirmation.md
docs/realtime-context-management-commercial-design.md
docs/realtime-context-real-chain-validation.md
docs/superpowers/plans/2026-05-22-realtime-context-first-stage.md
```

## 下一步

建议先暂停重复真实外呼，给线路侧提供排查信息：

```text
公网 IP：111.229.146.182
SIP proxy：47.94.86.132:5089
主叫：037123124845
被叫：185****8743
时间：2026-05-24 16:40:26-16:40:29 CST
SIP：508
Q.850 cause：31
FreeSWITCH call_id：7059405b9fcd4db59e760654481b2345
```

线路侧确认路由、白名单、主叫权限或被叫策略后，再初始化新 `call_record` 做下一通真实电话。
