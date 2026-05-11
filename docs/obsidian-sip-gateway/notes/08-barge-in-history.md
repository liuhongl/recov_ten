# 插话打断和 committed history

用户插话是实时电话机器人的核心难点。

## 为什么要清空本地播放队列

假设 AI 正在说：

```text
好的，我现在为您查询订单，请稍等...
```

用户中途说：

```text
不用查订单，我要改地址。
```

这时网关本地 [[07-playout-engine|Playout Engine]] 里可能还排着一堆旧回复。

如果不清空，本地还会继续往 FreeSWITCH 发旧音频，用户就会听到 AI 继续讲上一轮废话。

## 为什么还要 break FreeSWITCH

清空网关本地队列不够。

FreeSWITCH 侧可能已经缓存了一些旧音频，所以还要通过 [[03-freeswitch-event-socket|Event Socket]] 发命令：

```text
uuid_audio_stream <uuid> break
```

意思是：

```text
这通电话当前正在播的旧音频立刻停掉。
```

## committed history 是什么

`assistant turn` 是 AI 的一轮回复。

如果 AI 说了一半被用户打断：

```text
AI：好的，我帮您查询订单，您当前的订单状...
用户：不用了，我要改地址。
```

这半句话不能当作“已完成对话”写进历史。

否则下一轮模型会以为：

```text
我已经完整告诉用户订单状态了。
```

但事实是用户根本没听完。

所以规则是：

```text
只有确认完整播放给用户听到的 assistant turn 才进入 committed history。
被打断的 assistant turn 必须 abandoned，不能污染下一轮模型上下文。
```

## 尾部静音和播放完成确认

实时音频最容易出问题的是结尾。

如果模型说完后立刻认为结束，最后几个字可能还在缓冲区里。

常见问题：

```text
最后几个字没播出来
上一轮最后几个字混到下一轮开头才播出来
```

所以需要：

```text
尾部静音
  给播放链路一点 drain 时间。

播放完成确认
  等 FreeSWITCH 明确说这一轮播完了。
```

目标是：

```text
AI 说话稳定、不抖、不抢话；
用户插话时能立即停旧声音；
没完整播放的 AI 回复不进入模型历史；
每一轮语音边界干净。
```
