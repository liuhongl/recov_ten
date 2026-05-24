# sip-provider 真实线路 Profile 与沙箱测试边界

本文档沉淀当前 `sip-provider` 真实 SIP trunk 的已知边界，用于指导本地沙箱测试、真实线路联调和后续排障。

核心目标不是替代真实线路，而是在不拨真实电话的情况下，先验证我方外呼控制面、FreeSWITCH 出局配置、SIP/SDP 格式、失败码映射和接通后的媒体网关链路。

## 1. 当前结论

当前信息已经足够做第一版 `sip-provider` 沙箱。

第一版沙箱应覆盖：

```text
1. IP 白名单 trunk，register=false，Gateway 状态表现为 NOREG / UP。
2. 国内原始号码格式，不加 +86 / 86 / 0 / 9 前缀。
3. From URI 用户部分必须是允许显号。
4. PCMA/8000 必须支持，PCMU/8000 可兼容。
5. ptime=20ms，RTP/AVP，sendrecv。
6. RFC2833 DTMF，telephone-event payload=101。
7. 100 / 180 / 183 / 200 / 408 / 486 / 503 / 508 / 603 等响应场景。
8. 508 与 Q.850 cause=31 归类为上游或 trunk 侧失败。
```

第一版沙箱不应追求完整复刻公网 NAT、供应商 SBC 私有策略、真实运营商路由或真实用户接听行为。这些仍需要真实线路小样本验证。

## 2. SIP trunk 接入方式

已知信息：

```text
接入模式：IP 白名单
注册模式：否
Gateway：sip-provider
Gateway 状态：NOREG / UP
SIP proxy：47.94.86.132:5089
Realm：47.94.86.132
From domain：47.94.86.132
传输协议：UDP
认证方式：无 username/password
Password：no
本地公网 SIP：111.229.146.182:19000
本地内网地址：10.0.12.7
供应商 User-Agent：uincall_sbc
FreeSWITCH User-Agent：FreeSWITCH-mod_sofia/1.10.12
```

含义：

```text
这不是 MicroSIP 常见的账号密码注册模式。
FreeSWITCH 不需要向供应商注册账号，线路可用性依赖供应商侧 IP 白名单。
复用该线路的服务器公网 IP 必须加入供应商白名单。
SIP 响应从 47.94.86.132:5089 返回。
当前链路处于 NAT/rport 场景。
```

沙箱建议：

```text
沙箱第一版模拟 register=false 的 trunk。
配置层保留 sip-provider gateway 名称。
模拟 Gateway 状态为 NOREG / UP。
对非允许来源可模拟 403 Forbidden。
```

注意：本地沙箱不能真正代表供应商看到的公网出口 IP，除非沙箱部署到云上或专门做公网入口测试。

## 3. 被叫号码格式

已知信息：

```text
格式：国内号码原始格式
手机号示例：15800967789 / 18518968743
固话/显号示例：037123124845
不加：+86
不加：86
不加：0 / 9 出局前缀
Request-URI：sip:{callee}@47.94.86.132:5089
出局格式：sofia/gateway/sip-provider/{callee}
```

沙箱应接受：

```text
11 位国内手机号
已报备或明确允许的固话/显号格式
```

沙箱应拒绝：

```text
+8615800967789
8615800967789
1000
带 0 / 9 出局前缀的号码
空号码或非数字号码
```

建议失败响应：

```text
404 Not Found
484 Address Incomplete
```

## 4. caller_id 规则

已知信息：

```text
当前 caller_id：037123124845
From URI：sip:037123124845@47.94.86.132
caller-id-in-from：true
effective_caller_id_number：037123124845
effective_caller_id_name：037123124845
From display name：可能为 Anonymous
```

含义：

```text
caller_id 必须使用线路允许或报备的显号。
caller_id 主要放在 From URI 用户部分。
当前未确认 P-Asserted-Identity 必填。
Remote-Party-ID 在供应商响应中出现，但不能证明我方 INVITE 必填。
```

沙箱建议：

```text
校验 From URI 用户部分是否为允许显号。
允许 From display name 为 Anonymous。
不强制要求 P-Asserted-Identity、Remote-Party-ID、Diversion 或业务 X-Header。
caller_id 不合法时返回 403 Forbidden。
```

## 5. Codec、ptime 与 DTMF

已知信息：

```text
主要 codec：PCMA / G.711 A-law
PCMA payload type：8
兼容观察到：PCMU / G.711 μ-law
PCMU payload type：0
采样率：8000 Hz
ptime：20ms
RTP profile：RTP/AVP
媒体方向：sendrecv
DTMF：RFC2833 / telephone-event
DTMF payload：101
DTMF events：0-16
SRTP：未配置
视频：禁用
```

沙箱建议：

```text
PCMA/8000 必须支持。
PCMU/8000 可以作为兼容项。
ptime 按 20ms 校验。
RTP profile 按 RTP/AVP 校验。
媒体方向按 sendrecv 校验。
telephone-event/8000 payload 101 应支持。
不启用 SRTP，不启用视频。
```

重要边界：

```text
SIP trunk 侧 codec 是 PCMA/PCMU。
FreeSWITCH 到实时媒体网关 Bridge 内部是 L16 PCM / 8000 Hz。
两者不是同一层格式，不要混淆。
```

## 6. RTP 公网与 NAT

已知信息：

```text
供应商 RTP IP：47.94.86.132
已见供应商 RTP 端口：20900、22110
SDP c= 地址：IN IP4 47.94.86.132
本地公网 SIP：111.229.146.182:19000
本地内网收包：10.0.12.7
Strict RTP learned remote：47.94.86.132:20900
NAT/rport：确认存在
```

含义：

```text
FreeSWITCH 必须正确配置公网 SIP/RTP 地址或等价 NAT 映射。
真实线路大概率不能接受 SDP 中暴露不可达的私网媒体地址。
供应商完整 RTP 端口范围仍需线路方确认。
```

沙箱第一版建议：

```text
模拟公网 RTP 侧行为。
如果我方 SDP 出现私网 IP，可模拟拒绝或接通后单向音频。
固定使用少量 RTP 端口模拟即可，不必第一版复刻完整公网端口范围。
```

仍需真实线路验证：

```text
公网 NAT 映射是否正确。
供应商是否严格拒绝私网 SDP。
symmetric RTP 行为。
RTP 超时行为。
完整 RTP 端口范围。
```

## 7. 常见失败码与业务映射

已知和建议映射：

```text
100 Trying：已收到 INVITE -> trying
180 Ringing：振铃 -> ringing
183 Session Progress：呼叫进展 / 早媒体，可能携带 SDP -> progress_with_sdp
200 OK：接通 -> answered
400 Bad Request：请求格式错误 -> bad_request
401 Unauthorized：鉴权失败，当前 IP 白名单模式一般不应出现 -> forbidden
403 Forbidden：IP 未加白 / caller_id 未授权 / 权限或余额问题 -> forbidden
404 Not Found：号码不存在或路由不存在 -> invalid_number
408 Request Timeout：请求超时 / 无人接 / 上游超时 -> no_answer_or_timeout
480 Temporarily Unavailable：暂时无法接通 / 无人接 -> no_answer_or_timeout
484 Address Incomplete：号码格式不完整 -> invalid_number
486 Busy Here：忙线 -> busy
503 Service Unavailable：线路或网关不可用 -> trunk_unavailable
508：上游/SBC 未明原因失败 -> trunk_or_upstream_failure
603 Decline：拒接 -> rejected
604 Does Not Exist Anywhere：号码不存在 -> invalid_number
Q.850 cause=31：NORMAL_UNSPECIFIED -> normal_unspecified / trunk_or_upstream_failure
```

已被日志确认出现：

```text
100
183
408
508
Q.850 cause=31
```

沙箱第一版必须模拟：

```text
100 -> 183 with SDP -> 200
100 -> 183 with SDP -> 408
486 Busy Here
603 Decline
503 Service Unavailable
508 Upstream/SBC failure
404 / 484 invalid number
403 caller_id forbidden
```

## 8. 自定义 SIP Header

当前未发现我方必填：

```text
自定义必填 Header：未发现
P-Asserted-Identity：未发现
Diversion：未发现
业务 X-Header：未发现
sip_h_*：未发现
```

日志中出现但不代表我方 INVITE 必填：

```text
Remote-Party-ID
X-FS-Display-Name
X-FS-Display-Number
X-FS-Support
```

沙箱第一版：

```text
不强制自定义 header。
强制校验 From caller_id。
保留后续根据真实 SIP trace 增强 header 校验的空间。
```

## 9. 沙箱第一版测试场景

当前第一版已采用 FreeSWITCH-only 沙箱：本地 `external` profile 通过 `sip-provider-sandbox` gateway 发起 SIP INVITE 到同一个 FreeSWITCH 容器内的 `sip-provider-sandbox` profile，后者监听 UDP `5089`，使用 `sip_provider_sandbox` dialplan context 模拟供应商响应。

配置文件：

```text
freeswitch-local/conf/sip_profiles/external/sip-provider-sandbox.xml
freeswitch-local/conf/sip_profiles/sip-provider-sandbox.xml
freeswitch-local/conf/dialplan/sip_provider_sandbox.xml
```

Gateway 形态：

```text
Name：sip-provider-sandbox
State：NOREG
Status：UP
From：sip:037123124845@47.94.86.132
Proxy：sip:<container-local-ip>:5089
```

沙箱使用固定号码驱动：

```text
15800967789 -> 正常接通，100 -> 180 -> 200
18518968743 -> 183 with SDP 后接通，100 -> 183 -> 200
19900000000 -> 408 超时
19900000001 -> 486 忙线
19900000002 -> 603 拒接
19900000003 -> 508 上游失败，携带 Q.850 cause=31
19900000004 -> 503 trunk unavailable
+8615800967789 -> 484 号码格式错误
8615800967789 -> 484 号码格式错误
1000 -> 404 或 484 号码格式错误
caller_id 非 037123124845 -> 403 Forbidden
```

HTTP 外呼时可显式传入 endpoint：

```json
{
  "destination": "19900000003",
  "endpoint": "sofia/gateway/sip-provider-sandbox/19900000003",
  "caller_id_number": "037123124845",
  "caller_id_name": "037123124845",
  "originate_timeout_seconds": 8,
  "context": {
    "scene": "sip-provider-sandbox-upstream-508"
  }
}
```

也可以通过 `/outbound-test` 页面选择 `sip-provider 沙箱` 场景，页面会自动填写号码、主叫和 endpoint。

每个场景应验证：

```text
1. /calls 返回的 phase 是否正确。
2. sip_status / sip_reason / hangup_cause 是否记录。
3. failure_reason / failure_label / failure_hint 是否可读。
4. 183 不应被误判为 answered。
5. 200 后 media_connected 是否出现。
6. 接通后 9199 -> 实时媒体网关 -> 豆包 S2S 是否正常。
```

## 9.1 真实线路成功样本

2026-05-12，公网服务器 `111.229.146.182` 已完成真实线路接通验证：

```text
主叫：037123124845
被叫：18518968743
Call-ID：a5edb3c9-c883-123f-adaf-ceee8053b903
SIP proxy：47.94.86.132:5089
供应商 User-Agent：uincall_sbc
供应商 SDP：c=IN IP4 47.94.86.132
供应商 RTP：47.94.86.132:29092
Codec：PCMA/8000
ptime：20ms
最终阶段：media_connected
电话侧确认：可通话
```

该样本证明当前服务器、主叫号码、SIP/SDP、PCMA/8000、ptime=20 和接通后的媒体网关链路已经具备真实通话能力。

## 9.2 真实线路失败样本

2026-05-24，公网服务器 `111.229.146.182` 使用当前 Python 网关版本发起一通真实 `sip-provider` 外呼，供应商侧返回 `508 / Q.850 cause=31`：

```text
公网服务器：111.229.146.182
主叫：037123124845
被叫：185****8743
Python callId：7464233794932996429
FreeSWITCH call_id：7059405b9fcd4db59e760654481b2345
endpoint：sofia/gateway/sip-provider/185****8743
发起时间：2026-05-24 16:40:26 CST
结束时间：2026-05-24 16:40:29 CST
HTTP 状态：failed
phase：trunk_or_upstream_failure
SIP 状态：508
Q.850 reason：31
hangup_cause：NORMAL_UNSPECIFIED
media_connected_at_ms：空
call_record.status：2
transcript：空
```

该失败不是页面沙箱误发。请求实际进入真实 gateway：

```text
sofia/gateway/sip-provider/185****8743
```

FreeSWITCH 已发出 INVITE，并收到供应商 `uincall_sbc` 的 `183` 与远端 SDP。双方媒体参数匹配：

```text
我方 SDP c=IN IP4 111.229.146.182
我方 m=audio 16468 RTP/AVP 8 0 101
供应商 SDP c=IN IP4 47.94.86.132
供应商 m=audio 21696 RTP/AVP 8 101
Codec：PCMA/8000
ptime：20
telephone-event：101
```

随后供应商侧在 200 接听前终止呼叫：

```text
terminated [508]
Remote Reason：31
Originate Resulted in Error Cause：31 [NORMAL_UNSPECIFIED]
```

客观结论：

```text
1. 我方真实线路 endpoint、主叫、号码格式、SDP 公网地址、PCMA/8000 和 ptime=20 已进入供应商 SBC。
2. 呼叫未进入 200 OK，也没有进入实时媒体网关，因此没有 transcript。
3. 当前问题应优先交给线路侧按时间、主叫、被叫和 FreeSWITCH call_id 查询 CDR / SBC 路由原因。
4. 继续重复发起相同号码和主叫，大概率仍返回 508，除非线路侧确认路由或权限已调整。
```

## 10. 仍需补充的信息

这些不阻塞第一版沙箱，但会影响逼真度：

```text
1. 供应商完整 RTP 端口范围。
2. 更多成功呼叫的脱敏 SIP trace：INVITE / 100 / 183 / 200 / ACK / BYE。
3. 失败呼叫的脱敏 SIP trace：408 / 508 / 403 / 486 / 603。
4. codec 不匹配时真实返回 400、488、503 还是 508。
5. 508 响应里是否稳定携带 Reason / Q.850 / 私有 header。
6. Contact / Via / rport / SDP c= 对公网 IP 的严格要求。
7. 是否要求 OPTIONS ping，以及不通时 Gateway 状态如何变化。
```

## 11. 实施边界

建议现在做：

```text
1. 文档化 sip-provider profile。
2. 补齐 508 / Q.850 cause=31 业务映射。
3. 做最小版 sip-provider 沙箱。
4. 用 /outbound-test 跑固定沙箱场景。
```

建议暂缓：

```text
1. 完整公网 NAT 复刻。
2. 复杂 SBC 行为模拟。
3. 多线路路由。
4. 供应商私有 header 强校验。
5. RTP 丢包、抖动、乱序模型。
6. 号码池、权限、审批、配额系统。
```

## 12. 一句话总结

本沙箱的价值是先证明：

```text
我方按 sip-provider 的已知规则发起外呼时，号码、caller_id、SIP/SDP、codec、状态机和接通后的媒体网关链路基本正确。
```

真实线路最终仍要验证：

```text
供应商 SBC 私有策略、公网 NAT/RTP 行为、真实运营商路由和真实被叫侧行为。
```
