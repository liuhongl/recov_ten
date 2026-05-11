# Playout Engine 和播放控制

豆包 S2S 的音频输出不是电话可以直接稳定播放的帧流。

模型是一边生成一边吐音频片段；电话播放则要求按固定媒体时钟持续输出。

当前电话侧目标是：

```text
8k
mono
20ms
320 bytes PCM
```

## Playout Engine 做什么

```text
模型音频 delta
  -> float32 转 int16
  -> 重采样到 8k
  -> 切成 20ms / 320 bytes 电话帧
  -> 放入 Playout Engine
  -> 按稳定节奏发给 FreeSWITCH
```

它本质上是播放节拍器：

```text
不能模型来得快就狂发
不能模型来得慢就直接断流
要按电话媒体时钟稳定输出
```

## Jitter / prefill

模型返回音频的速度不稳定，有时快、有时慢。

如果模型刚吐一点就马上播放，电话侧可能出现：

```text
说两个字 -> 卡一下 -> 再说三个字 -> 又卡一下
```

`prefill` 是先攒一点音频再开始播。

`jitter buffer` 是播放时保留一点缓冲，抵消网络和模型输出的不稳定。

代价是增加一点延迟，换来播放更顺。

## FreeSWITCH playback 事件闭环

网关把音频发给 FreeSWITCH，不等于电话用户已经听完。

真实链路里至少有三层：

```text
网关发出去了
FreeSWITCH 收到了
电话用户真的听完了
```

所以需要等待 FreeSWITCH 的播放事件：

```text
chunk_played
queue_completed
```

这叫播放事件闭环。没有它，网关会误以为“我发完了 = 用户听完了”。

相关笔记：[[08-barge-in-history|插话打断和 committed history]]
