# Channel 事件和外呼状态机

`CHANNEL_PROGRESS`、`CHANNEL_ANSWER`、`CHANNEL_HANGUP`、`CHANNEL_HANGUP_COMPLETE` 都是 FreeSWITCH 自己产生的 channel 生命周期事件。

它们不是业务系统传进来的，也不是网关编出来的。

## 事件从哪里来

```text
一通电话在 FreeSWITCH 里会变成一个 channel。
channel 从创建、呼叫中、接通、挂断，到最终清理。
每发生一个关键状态变化，FreeSWITCH 就发布一个 CHANNEL_* 事件。
网关通过 Event Socket 订阅并消费这些事件。
```

## 当前状态机映射

```text
CHANNEL_PROGRESS / CHANNEL_PROGRESS_MEDIA -> ringing
CHANNEL_ANSWER                            -> answered
媒体 WebSocket 连接                        -> media_connected
CHANNEL_HANGUP / CHANNEL_HANGUP_COMPLETE  -> completed / busy / no_answer / failed
```

## 事件解释

```text
CHANNEL_PROGRESS
  通常来自 SIP 180 Ringing 或 183 Session Progress，表示呼叫有进展或正在响铃。

CHANNEL_ANSWER
  通常来自对端接听后的 SIP 200 OK，表示电话 channel 已接通。

CHANNEL_HANGUP
  表示通话进入挂断流程。原因可能是用户挂断、忙线、无人接听、线路失败或系统主动取消。

CHANNEL_HANGUP_COMPLETE
  表示 FreeSWITCH 已完成这条 channel 的挂断清理。
```

## answered 和 media_connected 的区别

```text
answered
  电话接通了。

media_connected
  9199 媒体 WebSocket 已经连到实时媒体网关，电话音频真正进入 AI 处理链路。
```

这两个状态必须分开。否则会误以为“用户接电话了 = AI 已经能听见用户说话了”，但事实不一定成立。

相关笔记：

- [[03-freeswitch-event-socket|FreeSWITCH 和 Event Socket]]
- [[05-media-path-9199|9199 媒体链路]]
- [[09-call-query-fields|外呼查询字段]]
