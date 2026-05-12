# 公网服务器 sip-provider 单路验证 Runbook

本文档用于把当前本地已验证的 `sip-provider` 沙箱成果，迁移到具备公网 SIP/RTP 可达性的服务器上做真实线路单路验证。

当前目标不是上线生产，也不是批量外呼，而是验证一条事实链：

```text
公网服务器 FreeSWITCH
  -> sip-provider 真实 SIP trunk
  -> 真实被叫
  -> 接通后进入 9199
  -> sip-realtime-voice-gateway
  -> 豆包 S2S
  -> 电话用户能双向对话
```

## 1. 前置条件

服务器必须满足：

```text
公网 IP：111.229.146.182，或供应商已加入白名单的等价公网 IP
系统：能运行 Docker / Docker Compose
代码：当前仓库分支 codex/feat/sip-provider-sandbox
密钥：服务器本地 .env 已配置豆包 S2S 等真实运行参数
SIP：供应商允许从该公网 IP 发起 UDP 19000 或实际配置端口
RTP：服务器安全组和系统防火墙放通 FreeSWITCH RTP 端口范围
```

本阶段不要在 Mac + Docker Desktop 上做真实线路最终验证。Mac 本地适合软电话和沙箱验证，但不代表供应商能访问本机 RTP。

## 2. 服务器端口

至少确认这些端口：

```text
9100/tcp       网关 HTTP 控制面，仅建议受信网络访问
8021/tcp       FreeSWITCH Event Socket，仅本机或受信网络访问
19000/udp      真实 SIP trunk 本地监听端口，如果使用 19000
16384-16484/udp 或实际 RTP 范围
```

如果沿用当前本地 Docker Compose，需要注意它默认暴露的是：

```text
5060/tcp+udp
5080/tcp+udp
5089/udp
18021/tcp -> 容器 8021
26384-26484/udp -> 容器 16384-16484
```

真实线路建议单独确认 `external` profile 的 SIP 端口是否要改为 `19000`。如果供应商白名单里记录的是 `111.229.146.182:19000`，则 FreeSWITCH `external_sip_port` 和 Docker 端口映射也要一致。

## 3. 获取代码

```bash
git fetch origin
git checkout codex/feat/sip-provider-sandbox
git pull
```

确认提交包含：

```text
feat(freeswitch): 增加 sip-provider 本地沙箱
docs(freeswitch): 增加真实 sip-provider gateway 模板
```

## 4. 配置公网 SIP/RTP 地址

编辑：

```text
freeswitch-local/conf/vars.xml
```

将本地 LAN 地址改为公网服务器地址：

```xml
<X-PRE-PROCESS cmd="set" data="external_rtp_ip=111.229.146.182"/>
<X-PRE-PROCESS cmd="set" data="external_sip_ip=111.229.146.182"/>
```

如果真实线路要求本地 SIP 端口为 `19000`，还需要确认：

```xml
<X-PRE-PROCESS cmd="set" data="external_sip_port=19000"/>
```

同时调整 Docker Compose 端口映射，确保宿主机 `19000/udp` 映射到容器的 `19000/udp` 或实际 FreeSWITCH profile 监听端口。

## 5. 启用真实 sip-provider gateway

模板默认不会被 FreeSWITCH 加载：

```text
freeswitch-local/conf/sip_profiles/external/sip-provider.xml.template
```

在公网服务器确认 SIP/RTP 条件后复制：

```bash
cp freeswitch-local/conf/sip_profiles/external/sip-provider.xml.template \
  freeswitch-local/conf/sip_profiles/external/sip-provider.xml
```

模板关键配置：

```text
gateway name：sip-provider
proxy：47.94.86.132:5089
realm：47.94.86.132
from-user：037123124845
from-domain：47.94.86.132
register：false
caller-id-in-from：true
codec：PCMA
```

## 6. 配置网关外呼 endpoint

服务器 `.env` 建议设置：

```env
OUTBOUND_ENDPOINT_TEMPLATE=sofia/gateway/sip-provider/{destination}
```

也可以在 `/outbound-test` 或 `POST /calls` 中显式传入：

```text
endpoint = sofia/gateway/sip-provider/{真实被叫号码}
```

显式传入 endpoint 更适合第一通验证，因为它不会影响默认本地软电话配置。

## 7. 启动和检查

```bash
scripts/dev-local.sh restart
scripts/dev-local.sh check
```

检查真实 gateway：

```bash
docker exec sip_realtime_freeswitch fs_cli -x "sofia status gateway sip-provider"
```

期望：

```text
Name     sip-provider
State    NOREG
Status   UP
From     <sip:037123124845@47.94.86.132>
Proxy    sip:47.94.86.132:5089
```

检查 external profile：

```bash
docker exec sip_realtime_freeswitch fs_cli -x "sofia status profile external"
```

重点确认：

```text
Ext-SIP-IP   111.229.146.182
Ext-RTP-IP   111.229.146.182
CODECS OUT   PCMA
TEL-EVENT    101
```

## 8. 第一通真实外呼

请求示例：

```bash
curl -sS http://127.0.0.1:9100/calls \
  -H 'Content-Type: application/json' \
  -d '{
    "destination": "15800967789",
    "external_call_id": "real-sip-single-001",
    "caller_id_number": "037123124845",
    "caller_id_name": "037123124845",
    "endpoint": "sofia/gateway/sip-provider/15800967789",
    "originate_timeout_seconds": 30,
    "context": {
      "scene": "real-sip-single-preflight"
    }
  }'
```

注意：真实测试号码必须由人工确认后替换，不要直接复制示例号码执行。

## 9. 查询和记录

```bash
curl -sS http://127.0.0.1:9100/calls/{call_id}
```

每通真实测试记录：

```text
call_id
external_call_id
destination 脱敏
endpoint
status / phase / phase_label
ringing_at_ms
answered_at_ms
media_connected_at_ms
completed_at_ms
hangup_cause
sip_status / sip_reason
failure_reason / failure_label
是否听到 AI
AI 是否听到用户
供应商 CDR 或 SIP trace 编号
```

## 10. 通过标准

真实单路验证通过必须同时满足：

```text
1. /calls 创建成功。
2. FreeSWITCH originate 有 +OK 或明确失败原因。
3. 接听时出现 CHANNEL_ANSWER。
4. 接通后 media_connected_at_ms 有值。
5. 电话用户能听到 AI。
6. AI 能听到电话用户。
7. 用户挂机后 status=completed，hangup_cause=NORMAL_CLEARING。
```

只接通但单向无声不算通过，因为 SIP 信令成立不等于 RTP 双向成立。

## 10.1 当前成功基线

2026-05-12 已在公网服务器完成一通真实线路接通验证：

```text
公网服务器：111.229.146.182
SIP proxy：47.94.86.132:5089
本地 SIP：111.229.146.182:19000/udp
主叫号码：037123124845
被叫号码：18518968743
Call-ID：a5edb3c9-c883-123f-adaf-ceee8053b903
供应商 User-Agent：uincall_sbc
供应商 SDP：47.94.86.132:29092
Codec：PCMA/8000
ptime：20ms
网关状态：media_connected
结果：电话侧确认可通话
```

该样本是后续排查的基线。若之后同样配置下没有供应商回包，优先对照供应商入口日志、IP 白名单和线路状态，而不是先改媒体网关。

## 11. 失败时先收集证据

不要同时改多个变量。失败时先收集：

```bash
docker exec sip_realtime_freeswitch fs_cli -x "show calls"
docker exec sip_realtime_freeswitch fs_cli -x "sofia status gateway sip-provider"
docker exec sip_realtime_freeswitch fs_cli -x "sofia status profile external"
```

再补充：

```text
网关 /calls/{call_id} 返回
FreeSWITCH 控制台日志
供应商 CDR
脱敏 SIP trace
```

优先按下面顺序归因：

```text
403：IP 白名单、caller_id 授权、线路权限
404 / 484 / 604：号码格式
408 / 480：无人接或上游超时
486：忙线
503：线路或网关不可用
508 / Q.850 cause=31：供应商 SBC、上游路由、公网 NAT/RTP
接通无声：SDP c= 地址、RTP 端口、防火墙、安全组、symmetric RTP
AI 不回：9199 媒体 WebSocket、实时媒体网关、豆包 S2S、VAD
```

## 12. 单通通过后的下一步

```text
1. 将成功样本补充回 docs/sip-provider-profile.md。
2. 再做 3 到 5 个小样本：正常接听、无人接、拒接、忙线、被叫挂机。
3. 不做批量外呼。
4. 小样本稳定后，再做业务回调、幂等、持久化和并发护栏。
```
