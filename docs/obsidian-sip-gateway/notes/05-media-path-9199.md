# 9199 媒体链路

`FreeSWITCH 9199 媒体链路` 是电话接通后的媒体入口。

## 它负责什么

```text
电话接通后进入 9199 拨号计划
FreeSWITCH 继续管理 SIP / RTP / PCMA / channel 生命周期
把电话里的音频流转给网关
把网关返回的音频播放给电话用户
```

可以把它理解成电话系统里的固定接线口：

```text
外呼也好，真实 SIP Trunk 也好，
只要电话接通后进入 9199，
后面的 AI 媒体处理路径就统一。
```

## 本地路径

```text
MicroSIP 1000
  -> FreeSWITCH
  -> 9199
  -> ws://host.docker.internal:9101/media/fs/{uuid}
  -> sip-realtime-voice-gateway 实时媒体网关
```

## 和 HTTP 控制面的区别

```text
HTTP 控制面
  管发起外呼、查询状态、挂断。

9199 媒体链路
  管电话接通后的声音怎么进入 AI 管道。
```

## 和实时媒体网关的区别

```text
9199 媒体链路
  电话侧媒体管道。

实时媒体网关
  AI 侧媒体适配和播放控制核心。
```

相关笔记：

- [[06-realtime-media-gateway|实时媒体网关]]
- [[07-playout-engine|Playout Engine 和播放控制]]
