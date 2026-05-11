# SIP 实时语音网关学习笔记

这组笔记解释当前独立 `sip-realtime-voice-gateway` 项目的核心概念。

## 推荐阅读顺序

1. [[notes/01-system-overview|系统总览]]
2. [[notes/02-control-plane|HTTP 控制面]]
3. [[notes/03-freeswitch-event-socket|FreeSWITCH 和 Event Socket]]
4. [[notes/04-channel-events|Channel 事件和外呼状态机]]
5. [[notes/05-media-path-9199|9199 媒体链路]]
6. [[notes/06-realtime-media-gateway|实时媒体网关]]
7. [[notes/07-playout-engine|Playout Engine 和播放控制]]
8. [[notes/08-barge-in-history|插话打断和 committed history]]
9. [[notes/09-call-query-fields|外呼查询字段]]
10. [[notes/10-troubleshooting|排障索引]]
11. [[notes/11-agent-collaboration|AI / Agent 协作规则]]
12. [[notes/12-sip-provider-profile|sip-provider 真实线路 Profile]]

## 外部项目文档

- [真实 SIP Trunk 单路预验证清单](../real-sip-trunk-preflight.md)
- [公网服务器 sip-provider 单路验证 Runbook](../public-server-sip-provider-runbook.md)

## 一句话理解

当前项目的本质是：

```text
业务系统通过 HTTP 控制面发起外呼；
FreeSWITCH 负责电话信令、SIP Trunk、RTP 和 9199 媒体接入；
sip-realtime-voice-gateway 负责通话状态、实时音频适配、豆包 S2S 连接、播放控制和打断控制。
```

## 主链路

```text
业务系统
  -> sip-realtime-voice-gateway HTTP 控制面
  -> FreeSWITCH / SIP Trunk
  -> 电话用户
  -> FreeSWITCH 9199 媒体链路
  -> sip-realtime-voice-gateway 实时媒体网关
  -> 豆包 S2S 端到端实时语音模型
  -> 电话用户
```

## 核心边界

```text
HTTP 控制面
  管叫谁、查状态、挂断。

FreeSWITCH / SIP Trunk
  管电话信令、线路、RTP、接通和挂断。

9199 媒体链路
  管电话接通后如何把音频接进网关。

实时媒体网关
  管电话音频和豆包 S2S 之间的格式转换、播放节奏、打断和历史。
```
