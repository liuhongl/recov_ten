# FreeSWITCH 和 Event Socket

## FreeSWITCH 的角色

FreeSWITCH 是电话网关。

它负责：

```text
SIP 信令
SIP Trunk 接入
RTP 音频
PCMA 协商
电话 channel 生命周期
接通和挂断
```

它不负责：

```text
AI 对话
豆包 S2S 调用
业务话术
模型上下文
播放队列历史管理
```

## Event Socket 是什么

Event Socket 是 FreeSWITCH 提供的控制和事件接口。

它不是 SIP，也不是 RTP 音频流。它更像 FreeSWITCH 暴露给外部系统的：

```text
远程控制台 + 事件订阅通道
```

当前项目里，它主要做三件事：

```text
1. 让网关命令 FreeSWITCH 发起外呼。
2. 让网关监听 FreeSWITCH 的通话状态事件。
3. 用户插话时，让网关命令 FreeSWITCH 停止旧音频播放。
```

## 和其他层的关系

```text
HTTP 控制面
  业务系统调用网关。

Event Socket
  网关控制 FreeSWITCH，并接收 FreeSWITCH 事件。

SIP / SIP Trunk
  FreeSWITCH 和电话网络之间建立、接通、挂断电话。

RTP / 媒体 WebSocket
  真正传输声音。
```

## 典型命令

发起外呼时，网关会通过 Event Socket 命令 FreeSWITCH 执行 originate。

用户插话时，网关会通过 Event Socket 发送：

```text
uuid_audio_stream <uuid> break
```

这用于停止 FreeSWITCH 侧已经缓存或正在播放的旧音频。

相关笔记：[[04-channel-events|Channel 事件和外呼状态机]]
