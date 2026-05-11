# 实时媒体网关

`sip-realtime-voice-gateway 实时媒体网关` 是电话音频和豆包 S2S 之间的翻译器与调度器。

## 上行：电话用户到模型

```text
电话用户说话
  -> FreeSWITCH / 9199
  -> 8k / 20ms 电话音频帧
  -> 实时媒体网关
  -> 转成豆包 S2S 需要的音频格式
  -> 豆包 S2S 实时语音模型
```

## 下行：模型到电话用户

```text
豆包 S2S 返回音频
  -> 实时媒体网关
  -> float32 转 int16
  -> 重采样到 8k
  -> 切成 20ms / 320 bytes 电话帧
  -> Playout Engine 稳定发送
  -> FreeSWITCH
  -> 电话用户听到
```

## 它解决的问题

```text
电话侧和模型侧音频格式不同
模型输出节奏不稳定
电话播放需要稳定媒体时钟
用户可能随时插话
旧音频不能继续播放
没播放完的 assistant 回复不能进入历史
每一轮回复尾字不能被吞，也不能串到下一轮
```

## 核心模块

- [[07-playout-engine|Playout Engine 和播放控制]]
- [[08-barge-in-history|插话打断和 committed history]]
- [[05-media-path-9199|9199 媒体链路]]
