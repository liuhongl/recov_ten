# 真实 SIP Trunk 单路预验证清单

本文档用于从 `sip-provider` 本地沙箱进入真实线路联调前做检查。目标不是做批量外呼系统，而是用最少真实电话次数确认：我方 FreeSWITCH 发出的 SIP/SDP 是否被供应商接受，RTP 是否双向可达，状态码和媒体链路是否能被网关正确记录。

## 1. 本阶段目标

真实线路单路预验证只验证这些事实：

```text
1. FreeSWITCH 能通过 sip-provider gateway 发出真实 INVITE。
2. 被叫号码格式和 caller_id 符合供应商规则。
3. 供应商接受 PCMA/8000、ptime=20、RTP/AVP、RFC2833 DTMF。
4. 本机公网 SIP/RTP 地址、NAT 和防火墙配置正确。
5. 接通后仍能进入 9199 媒体链路，并复用实时媒体网关。
6. 挂断、忙线、无人接、拒接和上游失败能落到可解释的状态字段。
```

不在本阶段解决：

```text
批量外呼
业务系统 webhook
号码池和审批流
并发压测
失败重试策略
完整运营商 CDR 对账系统
```

## 2. 发起前检查

### 2.0 当前本机预检结论

当前 Mac + Docker 本地环境用于软电话和 `sip-provider-sandbox` 验证，不适合作为真实运营商 SIP trunk 的最终验证环境：

```text
真实 sip-provider gateway：当前未启用
本机 external_sip_ip：192.168.0.100
本机 external_rtp_ip：192.168.0.100
预期公网 SIP/RTP：111.229.146.182 或等价公网映射
```

因此真实线路单通验证建议放到具备公网 SIP/RTP 可达性的服务器上执行。本仓库提供了不自动加载的 gateway 模板：

```text
freeswitch-local/conf/sip_profiles/external/sip-provider.xml.template
```

在真实服务器启用时，先确认公网 IP、白名单、防火墙和 RTP 端口范围，再复制为：

```text
freeswitch-local/conf/sip_profiles/external/sip-provider.xml
```

### 2.1 FreeSWITCH gateway

确认真实 gateway 存在且为 `NOREG / UP`：

```bash
docker exec sip_realtime_freeswitch fs_cli -x "sofia status gateway sip-provider"
```

重点看：

```text
Name：sip-provider
State：NOREG
Status：UP
Proxy：47.94.86.132:5089
From：sip:037123124845@47.94.86.132
Password：no
```

如果不是 `UP`，先不要发起真实外呼。优先检查 proxy、IP 白名单、防火墙和 FreeSWITCH profile 是否加载。

### 2.2 号码和主叫

真实外呼请求使用国内原始号码：

```text
被叫手机号：11 位手机号，例如 15800967789
不加：+86
不加：86
不加：0 / 9 出局前缀
caller_id_number：037123124845
```

`caller_id_number` 必须是供应商允许或已报备显号。caller_id 错误时，真实线路可能返回 `403 Forbidden`，也可能返回供应商私有失败码。

### 2.3 SIP/SDP 媒体参数

本阶段期望：

```text
Codec：PCMA / G.711 A-law
Payload type：8
采样率：8000 Hz
ptime：20ms
RTP profile：RTP/AVP
媒体方向：sendrecv
DTMF：RFC2833 / telephone-event
DTMF payload：101
SRTP：关闭
视频：关闭
```

如果供应商返回 codec 相关失败，先抓 SIP trace，确认我方 INVITE 的 SDP 是否包含 PCMA、telephone-event 和 ptime。

### 2.4 公网 NAT / RTP

真实线路最容易失败的是 RTP，而不是 HTTP 控制面。发起前确认：

```text
FreeSWITCH external_sip_ip 指向 111.229.146.182 或等价公网映射
FreeSWITCH external_rtp_ip 指向 111.229.146.182 或等价公网映射
服务器安全组 / 防火墙放通 SIP UDP 19000
服务器安全组 / 防火墙放通 FreeSWITCH RTP 端口范围
供应商白名单包含当前公网 IP
```

如果 SIP 已接通但无声音，优先看 SDP `c=` 地址和 RTP 端口是否是供应商可达地址。

## 3. 单通真实外呼流程

### 3.1 启动本地项目

```bash
scripts/dev-local.sh restart
scripts/dev-local.sh check
```

确认 gateway health 正常、FreeSWITCH healthy、RTP relay 或服务器 RTP 入口符合当前部署结构。

### 3.2 发起真实外呼

建议先用 `/outbound-test` 页面，显式填写 endpoint：

```text
endpoint = sofia/gateway/sip-provider/{真实被叫号码}
caller_id_number = 037123124845
caller_id_name = 037123124845
originate_timeout_seconds = 30
```

等价 HTTP 请求：

```json
{
  "destination": "15800967789",
  "external_call_id": "real-sip-single-001",
  "caller_id_number": "037123124845",
  "caller_id_name": "037123124845",
  "endpoint": "sofia/gateway/sip-provider/15800967789",
  "originate_timeout_seconds": 30,
  "context": {
    "scene": "real-sip-single-preflight"
  }
}
```

### 3.3 查询结果

```bash
curl -s http://127.0.0.1:9100/calls/{call_id}
```

重点记录：

```text
status
phase
phase_label
ringing_at_ms
answered_at_ms
media_connected_at_ms
completed_at_ms
answer_latency_ms
talk_duration_ms
hangup_cause
sip_status
sip_reason
failure_reason
failure_label
last_event_name
```

## 4. 通过标准

第一通真实线路验证通过，需要同时满足：

```text
1. /calls 成功创建 call_id。
2. FreeSWITCH originate 返回 +OK 或能进入明确失败状态。
3. 正常接听时出现 CHANNEL_ANSWER。
4. 接通后 media_connected_at_ms 有值。
5. 电话用户能听到 AI 回复，AI 能听到用户说话。
6. 用户挂机后 status=completed，hangup_cause=NORMAL_CLEARING。
7. 忙线、拒接、无人接或上游失败时，phase 和 failure_label 可解释。
```

如果只接通但单向无声，不算通过。真实电话链路的本质是 SIP 信令和 RTP 媒体都成立，只有一个成立不够。

### 4.1 当前成功样本

2026-05-12，公网服务器 `111.229.146.182` 已完成真实线路接通验证：

```text
主叫：037123124845
被叫：18518968743
Call-ID：a5edb3c9-c883-123f-adaf-ceee8053b903
SIP proxy：47.94.86.132:5089
供应商 User-Agent：uincall_sbc
供应商 RTP：47.94.86.132:29092
Codec：PCMA/8000
ptime：20ms
最终阶段：media_connected
电话侧确认：可通话
```

该样本说明当前服务器、SIP trunk、主叫号码、PCMA/8000、ptime=20 和媒体接入链路已经具备真实通话能力。

## 5. 失败归因速查

```text
403 Forbidden
  优先看：供应商 IP 白名单、caller_id 是否授权、线路权限或余额。

404 / 484 / 604
  优先看：被叫号码格式，是否误加 +86 / 86 / 出局前缀。

408 / 480
  优先看：被叫未接、上游超时、运营商路由超时。业务上归类为 no_answer。

486
  优先看：被叫忙线。业务上归类为 busy。

503
  优先看：供应商网关不可用、线路不可用、本地 NAT/路由不可达。

508 / Q.850 cause=31
  优先看：供应商 SBC 或上游路由未明失败、NAT/RTP、公网可达性和运营商 CDR。

接通但无声
  优先看：SDP c= 地址、RTP 端口、防火墙、安全组、symmetric RTP、供应商 RTP 来源端口。

AI 没回复但电话有声音
  优先看：9199 媒体 WebSocket、实时媒体网关日志、豆包 S2S 会话、VAD 和播放队列。
```

## 6. 每次真实测试需要保存的信息

每一通真实测试至少保存：

```text
测试时间
被叫号码脱敏值
caller_id
call_id
external_call_id
endpoint
最终 status / phase
hangup_cause
sip_status / sip_reason
是否听到 AI
AI 是否听到用户
供应商 CDR 或 SIP trace 编号
现象备注
```

建议将成功和失败各保留一份脱敏 SIP trace：

```text
INVITE
100 Trying
180 Ringing
183 Session Progress
200 OK
ACK
BYE
失败响应，例如 408 / 486 / 503 / 508
```

## 7. 下一步决策

如果单通真实线路通过：

```text
1. 固化真实 sip-provider gateway 配置。
2. 将成功和失败样本补充回 docs/sip-provider-profile.md。
3. 再做 3 到 5 个小样本场景：正常接听、无人接、拒接、忙线、被叫挂机。
4. 然后再进入业务回调、幂等、持久化和并发护栏。
```

如果单通真实线路失败：

```text
1. 不做批量外呼。
2. 先拿 call_id、FreeSWITCH SIP trace、供应商 CDR 对齐同一通电话。
3. 只修一个最小问题，再重新打一通。
4. 不要同时改号码格式、caller_id、codec、NAT 和 gateway 配置，否则无法判断根因。
```
