# 排障索引

## 电话没打出去

优先看：

```text
GET /calls/{call_id}
freeswitch_reply
error
endpoint
failure_reason
```

判断方向：

```text
endpoint 是否解析正确
FreeSWITCH Event Socket 是否可连接
originate 是否返回错误
SIP Trunk 或本地分机是否可用
```

相关：[[02-control-plane|HTTP 控制面]]

## 电话响了但没接通

优先看：

```text
status
phase
ringing_at_ms
answered_at_ms
hangup_cause
sip_status
```

常见映射：

```text
USER_BUSY -> 对方忙线或拒接，常见 SIP 486
NO_ANSWER -> 无人接听
NORMAL_TEMPORARY_FAILURE -> 临时失败，常见 SIP 503
```

相关：[[04-channel-events|Channel 事件和外呼状态机]]

## 电话接通但 AI 没声音

优先区分：

```text
answered 是否出现
media_connected 是否出现
```

如果只有 `answered`，没有 `media_connected`：

```text
电话接通了，但 9199 媒体 WebSocket 没有接到网关。
```

检查：

```text
FreeSWITCH 9199 拨号计划
ws://host.docker.internal:9101/media/fs/{uuid}
网关 9101 是否监听
Docker 到宿主机网络是否可达
```

相关：[[05-media-path-9199|9199 媒体链路]]

## macOS 本机软电话接通但没有声音

本次已确认根因：

```text
当前独立项目缺少 TEN 本地方案里的 rtp_host_relay.py 媒体转发层，
并且 Docker RTP 端口映射方式不匹配，
导致 Linphone 发到 SDP 广告端口的 RTP 没有以 FreeSWITCH 可接受的方式进入容器。
```

故障表现：

```text
9197 可以听到持续哔声
  -> FreeSWITCH 到软电话的下行 RTP 正常。

9196 没有回声，9199 没有 AI 声音
  -> 软电话到 FreeSWITCH 的上行 RTP 异常。
```

FreeSWITCH 证据：

```text
故障时：
  rtp_audio_in_packet_count = 0
  rtp_audio_in_media_bytes = 0
  rtp_audio_in_skip_packet_count 在涨

恢复 relay 后：
  rtp_audio_in_packet_count = 212
  rtp_audio_in_media_bytes = 36464
```

当前正确结构：

```text
Linphone / 本地软电话
  -> SIP: 192.168.0.100:5060
  -> RTP: 192.168.0.100:16384-16484

宿主机 rtp_host_relay.py
  -> 监听 16384-16484
  -> 转发到 127.0.0.1:26384-26484

Docker Desktop
  -> 26384-26484 映射到容器 16384-16484

FreeSWITCH
  -> 9196 echo
  -> 或 9199 mod_audio_stream
```

关键配置：

```yaml
ports:
  - "26384-26484:16384-16484/udp"
```

relay 命令：

```bash
python3 freeswitch-local/scripts/rtp_host_relay.py
```

排查顺序：

```text
1. 先拨 9197，确认下行能听到持续哔声。
2. 再拨 9196，确认上行回声。
3. 如果 9197 有声但 9196 无声，看 rtp_host_relay.py 是否在跑。
4. 看 Docker 是否映射 26384-26484 到容器 16384-16484。
5. 看 uuid_set_media_stats 后 rtp_audio_in_packet_count 是否大于 0。
6. 9196 正常后再测 9199，避免误判豆包或网关。
```

不要再把这个问题优先归因到：

```text
豆包 S2S
ASR / LLM / TTS
Mac 麦克风权限
Linphone 账号注册
网关 HTTP 控制面
```

这些都可能出问题，但本次根因已经由 TEN 可用配置、FreeSWITCH RTP 统计和 9196/9199 复测闭环确认。

## AI 声音卡顿

优先看：

```text
playback_underruns
max_playback_send_gap_ms
playback_send_gap_overruns
playback_prefill_frames
PLAYBACK_JITTER_BUFFER_MS
```

判断方向：

```text
模型输出是否抖动
网关发送节奏是否稳定
jitter buffer 是否太小
Python / Docker 调度是否出现 send gap
```

相关：[[07-playout-engine|Playout Engine 和播放控制]]

## 插话后旧声音还在播

优先看：

```text
本地播放队列是否清空
uuid_audio_stream <uuid> break 是否成功
freeswitch_break_failures
realtime_interrupt_failures
gateway_history_abandoned_turns
```

判断方向：

```text
只清网关队列不够，还必须停 FreeSWITCH 侧旧音频。
被打断的 assistant turn 不能进入 committed history。
```

相关：[[08-barge-in-history|插话打断和 committed history]]

## 最后几个字被吞或串到下一轮

优先看：

```text
TTSFinished / 359 是否收到
tail_silence_frames
chunk_played / queue_completed 是否收到
assistant turn 是否过早 committed
```

判断方向：

```text
模型生成完成不等于电话播放完成。
需要尾部静音 drain，更需要 FreeSWITCH 真实播放完成事件闭环。
```

相关：

- [[07-playout-engine|Playout Engine 和播放控制]]
- [[08-barge-in-history|插话打断和 committed history]]
