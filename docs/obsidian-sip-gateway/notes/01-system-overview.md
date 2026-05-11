# 系统总览

## 网关指什么

文档里说的“网关”，指当前仓库的 `sip-realtime-voice-gateway` 服务，也就是：

```bash
python -m app.main
```

它不是 [[03-freeswitch-event-socket|FreeSWITCH]]，不是豆包 S2S，也不是业务系统。它是夹在电话系统和实时语音模型中间的控制与媒体适配服务。

## 两个面

```text
HTTP 控制面
  接收业务系统外呼请求
  查询外呼状态
  挂断电话
  维护通话生命周期

实时媒体网关
  接收 FreeSWITCH 送来的电话音频
  连接豆包 S2S
  做音频格式转换
  做播放控制
  做用户插话打断
  做会话历史管理
```

## 第一性原理

业务系统真正关心的是：

```text
我要给这个用户打一通电话，并知道这通电话的结果。
```

业务系统不应该直接关心：

```text
FreeSWITCH originate 命令怎么拼
SIP endpoint 怎么解析
9199 拨号计划怎么写
Event Socket 怎么订阅事件
媒体 WebSocket 怎么接
PCMA / PCM / 20ms 帧怎么处理
```

所以中间需要 `sip-realtime-voice-gateway` 把这些电话工程细节封装成业务系统能理解的接口。

## 关联笔记

- [[02-control-plane|HTTP 控制面]]
- [[05-media-path-9199|9199 媒体链路]]
- [[06-realtime-media-gateway|实时媒体网关]]
