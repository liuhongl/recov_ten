# sip-provider 真实线路 Profile

这篇笔记用于快速理解真实 `sip-provider` 线路的关键规则。完整项目文档见：`docs/sip-provider-profile.md`。

## 当前结论

当前信息已经足够做第一版沙箱。沙箱的目标不是替代真实线路，而是在不拨真实电话的情况下，先验证我方外呼控制面、FreeSWITCH 出局配置、SIP/SDP 格式、失败码映射和接通后的媒体网关链路。

## 核心规则

```text
接入模式：IP 白名单
注册模式：否
Gateway：sip-provider
Gateway 状态：NOREG / UP
SIP proxy：47.94.86.132:5089
传输协议：UDP
认证方式：无 username/password
本地公网 SIP：111.229.146.182:19000
当前是 NAT/rport 场景
```

号码规则：

```text
使用国内号码原始格式
不加 +86 / 86 / 0 / 9 前缀
Request-URI：sip:{callee}@47.94.86.132:5089
出局格式：sofia/gateway/sip-provider/{callee}
```

caller_id 规则：

```text
当前 caller_id：037123124845
From URI：sip:037123124845@47.94.86.132
caller-id-in-from：true
caller_id 必须使用线路允许或报备的显号
```

媒体规则：

```text
PCMA / G.711 A-law，payload type 8
PCMU / G.711 μ-law 可作为兼容项，payload type 0
采样率：8000 Hz
ptime：20ms
RTP profile：RTP/AVP
媒体方向：sendrecv
DTMF：RFC2833 / telephone-event，payload type 101
SRTP：未配置
视频：禁用
```

## 沙箱第一版应覆盖

```text
1. IP 白名单 trunk，register=false。
2. 只接受国内原始号码格式。
3. 校验 From URI 用户部分必须是允许显号。
4. PCMA/8000 必须支持，PCMU/8000 可兼容。
5. ptime=20ms，RTP/AVP，sendrecv。
6. RFC2833 DTMF，telephone-event payload=101。
7. 模拟 100 / 180 / 183 / 200 / 408 / 486 / 503 / 508 / 603。
8. 将 508 与 Q.850 cause=31 归类为上游或 trunk 侧失败。
```

## 当前已落地的沙箱

第一版采用 FreeSWITCH-only 方式模拟 `sip-provider`：

```text
external profile
  -> gateway sip-provider-sandbox
  -> sip-provider-sandbox profile，监听 UDP 5089
  -> sip_provider_sandbox dialplan context
```

核心文件：

```text
freeswitch-local/conf/sip_profiles/external/sip-provider-sandbox.xml
freeswitch-local/conf/sip_profiles/sip-provider-sandbox.xml
freeswitch-local/conf/dialplan/sip_provider_sandbox.xml
```

验证命令：

```bash
docker exec sip_realtime_freeswitch fs_cli -x "sofia status gateway sip-provider-sandbox"
docker exec sip_realtime_freeswitch fs_cli -x "sofia status profile sip-provider-sandbox"
```

期望看到：

```text
Gateway：sip-provider-sandbox
State：NOREG
Status：UP
From：sip:037123124845@47.94.86.132
Profile codec：PCMA,PCMU
DTMF：RFC2833 / 101
```

`/outbound-test` 页面已经增加 `sip-provider 沙箱` 场景下拉，可以直接选择正常接通、183 后接通、408 超时、486 忙线、603 拒接、508 上游失败、503 线路不可用、号码格式错误和 caller_id 未授权。

## 仍需真实线路验证

```text
供应商完整 RTP 端口范围
公网 NAT 映射是否正确
供应商是否严格拒绝私网 SDP
symmetric RTP 行为
RTP 超时行为
供应商 SBC 私有策略
真实运营商路由
真实被叫侧行为
```

## 一句话理解

```text
sip-provider 沙箱验证的是“我方是否按真实线路的已知规则说对了 SIP/SDP 语言”；
真实线路验证的是“供应商和运营商是否真的接受并稳定处理这套说法”。
```
