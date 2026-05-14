# 本地网页外呼接通后无回复根因记录

本文档记录 2026-05-13 本地验证中出现的现象、证据和结论：

```text
软电话直接拨 9199：AI 可以回复
网页 http://127.0.0.1:9100/ 发起外呼：曾出现接通后说话无回复
```

## 1. 最终结论

根因不是豆包模型、提示词或 9199 网关主逻辑，而是本机 IP 变化后，本地 FreeSWITCH 的 SIP/RTP 对外地址与 Linphone 注册/媒体路径没有同步刷新。

网页外呼依赖 FreeSWITCH 当前的 `1000` 注册 Contact：

```text
网页 POST /calls
  -> 网关通过 Event Socket 执行 originate
  -> FreeSWITCH 解析 sofia_contact:*/1000
  -> 呼叫 Linphone
  -> 接通后 transfer 到 9199
  -> mod_audio_stream 把媒体推给网关
```

直拨 `9199` 不依赖 `sofia_contact:*/1000`：

```text
Linphone 直接拨 9199
  -> FreeSWITCH 直接进入 9199 dialplan
  -> mod_audio_stream 把媒体推给网关
```

因此，当 `1000` 的注册 Contact 或媒体路径处于旧 IP / 未刷新 / 未注册状态时，网页外呼会失败或接通后没有有效上行语音；但直拨 `9199` 仍可能正常。

## 2. 必要本地修正

当前机器局域网 IP 从旧值：

```text
192.168.0.100
```

变为：

```text
192.168.0.109
```

需要更新：

```text
freeswitch-local/conf/vars.xml
```

关键值：

```xml
<X-PRE-PROCESS cmd="set" data="domain=192.168.0.109"/>
<X-PRE-PROCESS cmd="set" data="external_rtp_ip=192.168.0.109"/>
<X-PRE-PROCESS cmd="set" data="external_sip_ip=192.168.0.109"/>
```

修改后必须重启 FreeSWITCH，让运行态配置生效。

## 3. 证据链

### 3.1 网页外呼请求确实发出

日志显示网页点击后，网关收到了外呼请求：

```text
outbound_call_queued destination=1000 endpoint=sofia_contact:*/1000
outbound_call_originate_finished ... reply=+OK
```

说明不是网页按钮没生效。

### 3.2 接通后无回复时，模型没有开始一轮对话

异常通话的网关指标：

```text
media_connected_at_ms: 已连接
inbound_frames=492
streamed_model_input_bytes=314880
turns_started=0
outbound_frames=0
```

含义：

```text
FreeSWITCH 到网关的 WebSocket 已连接
网关收到了音频帧并送给实时模型
但服务端 VAD 没识别到有效用户语音
所以模型没有生成回复音频
```

### 3.3 Linphone 重新注册后，网页外呼恢复

重新注册成功后，FreeSWITCH 能解析到新的 Contact：

```text
1000@192.168.0.109
sofia/internal/sip:1000@192.168.65.1:<port>;transport=udp;fs_nat=yes
```

随后网页外呼成功通话出现正常指标：

```text
turns_started > 0
outbound_frames > 0
input_transcript 有内容
output_transcript 有内容
```

示例成功通话：

```text
call_id=8f15955a272e4e07ab3b4350fba9b8d4
turns_started=6
outbound_frames=1280
```

这证明网页外呼链路本身可以工作，问题出在本地 IP / 注册 Contact / 媒体路径状态，而不是网关业务逻辑。

### 3.4 未注册时，网页外呼无法调起软电话

重启 FreeSWITCH 后，如果 Linphone 没有重新注册，`show registrations` 为：

```text
0 total
```

网页外呼失败原因：

```text
could not resolve FreeSWITCH contact for */1000: error/user_not_registered
```

这时外呼根本没有真正打到软电话。

## 4. 快速排查清单

### 4.1 确认本机 IP

```bash
ipconfig getifaddr en0
```

确认 `freeswitch-local/conf/vars.xml` 中的 `domain`、`external_rtp_ip`、`external_sip_ip` 与当前 LAN IP 一致。

### 4.2 重启本地链路

```bash
scripts/dev-local.sh restart
```

### 4.3 确认 FreeSWITCH 运行态 IP

```bash
docker exec sip_realtime_freeswitch fs_cli -x 'eval $${domain}'
docker exec sip_realtime_freeswitch fs_cli -x 'eval $${external_sip_ip}'
docker exec sip_realtime_freeswitch fs_cli -x 'eval $${external_rtp_ip}'
```

期望都返回当前本机 LAN IP。

### 4.4 确认 Linphone 注册

```bash
docker exec sip_realtime_freeswitch fs_cli -x 'show registrations'
```

期望至少有：

```text
1000@<当前 LAN IP>
```

如果是 `0 total`，网页外呼一定无法调起软电话。

### 4.5 确认网页外呼状态

```bash
curl -sS 'http://127.0.0.1:9100/calls?limit=5'
```

重点看：

```text
status
endpoint
answered_at_ms
media_connected_at_ms
hangup_cause
error
```

### 4.6 判断是否进入模型回复

查看网关日志：

```bash
rg -n 'realtime_phone_server_vad_speech_started|turn_done|freeswitch_realtime_session_finished' artifacts/logs/gateway.err.log
```

正常应看到：

```text
realtime_phone_server_vad_speech_started
realtime_phone_server_vad_turn_done
input_transcript=...
output_transcript=...
outbound_frames > 0
```

如果看到：

```text
turns_started=0
outbound_frames=0
```

说明没有识别到有效用户语音，问题应继续从 SIP/RTP 媒体路径和软电话音频输入排查，而不是改提示词。

## 5. 本地验证注意事项

- 每次本机 IP 变化后，都要同步更新 FreeSWITCH SIP/RTP 地址并重启容器。
- 每次重启 FreeSWITCH 后，都要确认 Linphone 已重新注册。
- 网页外呼依赖 `sofia_contact:*/1000`，注册状态是前置条件。
- 直拨 `9199` 能通，只能证明 `9199 -> 网关 -> 豆包` 主链路可用，不能证明网页外呼的 `1000` Contact 和媒体路径一定可用。
- 不要把 `turns_started=0` 误判为模型或提示词问题；它发生在模型生成回复之前。

