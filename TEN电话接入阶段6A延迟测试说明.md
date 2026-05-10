# TEN电话接入阶段6A延迟测试说明

## 1. 文档目的

本文档记录 2026-05-09 对“本地 FreeSWITCH 电话接入 TEN 最小 AI 链路”的一次延迟分析结果，方便后续同事理解当前测试背景、链路状态、实际耗时分布，以及为什么用户侧会感觉 AI 回复大约需要 5 秒。

本文只描述本地阶段 6A 的延迟测试情况，不代表真实 SIP trunk、生产 FreeSWITCH 或公网部署后的最终指标。

## 2. 测试背景

阶段 6A 的目标是验证本地电话呼入后，可以走完整的最小 AI 对话链路：

```text
MicroSIP
  -> FreeSWITCH 9199
  -> mod_audio_stream
  -> Media Hub
  -> sip_media_bridge
  -> Deepgram ASR
  -> sip_trunk_dialog_controller
  -> DeepSeek/OpenAI-compatible LLM
  -> ElevenLabs TTS
  -> sip_media_bridge
  -> Media Hub
  -> FreeSWITCH
  -> MicroSIP
```

本阶段不是验证真实运营商 SIP trunk，而是验证“电话媒体已经可以进入 TEN，并经 ASR/LLM/TTS 后回到电话侧”。

## 3. 当前实现方式

当前阶段 6A 不是全链路端到端实时语音模型，而是典型级联链路：

```text
音频流 -> ASR -> 文本 -> LLM -> 文本 -> TTS -> 音频流
```

更准确地说：

```text
电话音频实时进入 ASR
ASR 会产出 interim 与 final 结果
当前 dialog_controller 只在 final=True 后触发 LLM
LLM 使用流式输出
dialog_controller 按句子/标点把 LLM 文本送给 TTS
TTS 流式产出 PCM 音频
sip_media_bridge 按 20ms / 320 bytes 分片回传电话侧
```

因此，当前主要等待点是：

```text
用户说话 -> ASR 判定 final -> LLM 开始生成 -> TTS 首包 -> 电话播放
```

也就是说，**当前不会在 ASR interim 阶段就开始正式播放 AI 回复**。

## 4. 本次测试环境

测试日期：

```text
2026-05-09
```

本地组件：

```text
Docker Desktop
ten_agent_dev
ten_local_freeswitch
MicroSIP
```

补充说明：

```text
2026-05-09 后续又在 macOS + Linphone 上完成了一次 6A 复测。
原 Windows + MicroSIP 延迟样本仍作为 baseline。
macOS 复测用于确认换软电话和换宿主机后，6A 最小 AI 链路仍可打通。
```

电话拨号：

```text
9199
```

测试通道：

```text
fs_stage5a_local
```

TEN graph：

```text
voice_assistant_sip_trunk_cn_ai_minimal
```

本次日志文件：

```text
/tmp/sip_trunk_api_stage6a_latency.log
/tmp/sip_trunk_media_hub_stage6a_latency.log
```

注意：容器日志时间为 UTC。本地时间为 Asia/Shanghai，因此日志中的 `01:09` 对应本地 `09:09`。

## 5. 测试前发现的环境问题

测试前发现 Docker Desktop 重启后，FreeSWITCH 和 MicroSIP 的本地地址仍指向旧 IP：

```text
旧 IP: 192.168.0.165
当前 Windows WLAN IP: 192.168.43.52
```

注意：这里的 IP 只是本次测试现场值，不是接手同事必须使用的固定值。复现时应以当前电脑的 WLAN/以太网 IPv4 为准，并同步更新 FreeSWITCH 与 MicroSIP。

该问题会导致 MicroSIP 无法正常注册，或者注册/媒体路由不可靠。

已做的本地环境修正：

```text
FreeSWITCH vars.xml:
  domain=192.168.43.52
  external_rtp_ip=192.168.43.52
  external_sip_ip=192.168.43.52

MicroSIP.ini:
  server=192.168.43.52
  domain=192.168.43.52
```

修正后 FreeSWITCH 注册结果：

```text
1000,192.168.43.52,...,sofia/internal/sip:1000@192.168.43.52:58259,...
1 total
```

这个修正只影响本地测试环境，不属于 TEN 核心代码逻辑。

### 5.1 macOS + Linphone 复测发现的问题

macOS 复测时使用：

```text
host-lan-ip: 192.168.0.100
softphone: Linphone
SIP account: 1000
测试号码:
  9188 tone
  9189 echo
  9199 最小 AI 电话闭环
```

复测过程中发现：

```text
1. Linphone 播放设备曾指向外接显示器 / HDMI，导致下行有音频但用户听不到。
2. Docker Desktop for macOS 的 UDP/NAT 行为会改变 RTP 源端口，Linphone 可能丢弃非预期 RTP。
3. 首次 9199 验证时 TEN worker 已超时退出，Media Hub 出现 no_peer。
```

对应修正：

```text
1. Linphone playback / ringer / media device 改为 MacBook Pro 内置扬声器。
2. 增加 macOS 本机 RTP relay：
   host 16384-16484/udp -> 127.0.0.1:26384-26484 -> FreeSWITCH container 16384-16484/udp
3. 重新用较长 timeout 启动 TEN worker。
```

修正后验证结果：

```text
9188：能听到测试音
9189：能听到回声
9199：ASR 识别出用户实际说话内容，并有 TTS 回传

ASR final 样例：
  你好呀
  今天天气不错有大风
  好吧挂断吧
```

这个 macOS 修正只影响本机测试环境，不属于 TEN 核心代码逻辑，也不要求 Windows 复现时默认使用 RTP relay。

## 6. 本次测试样本

用户通过 MicroSIP 拨打：

```text
9199
```

用户说话内容：

```text
你好呀
```

ASR 识别结果：

```text
interim: 你好呀
final:   你好呀
```

AI 回复文本：

```text
你好！有什么可以帮你的吗？
```

## 7. 关键时间线

下面时间为容器日志时间，单位精确到毫秒级别。

```text
01:09:09.995  电话音频进入 Media Hub，并开始转发到 TEN
01:09:12.284  ASR interim 识别出“你好呀”
01:09:12.807  ASR final 识别出“你好呀”
01:09:12.808  dialog_controller 发起 LLM 请求
01:09:15.090  LLM 第一段可送 TTS 文本：“你好！”
01:09:15.220  LLM 完整回复完成：“你好！有什么可以帮你的吗？”
01:09:15.872  ElevenLabs TTS 产生首个音频事件
01:09:15.874  Media Hub 第一包 AI 音频回传 FreeSWITCH
01:09:18.305  Media Hub 最后一批 AI 音频回传结束
```

## 8. 耗时拆解

按本次样本拆解：

```text
电话音频进入 -> ASR final:
  约 2.8s

ASR final -> LLM 第一段文本:
  约 2.28s

LLM 第一段文本 -> TTS 首包:
  约 0.78s

TTS 首包 -> 完整语音播放完:
  约 2.43s

ASR final -> 电话侧听到第一声 AI:
  约 3.07s

电话音频进入 -> 电话侧听到第一声 AI:
  约 5.88s
```

## 9. 结论

本次约 5 秒的体感延迟是真实存在的，但主要不在 FreeSWITCH、Media Hub 或 sip_media_bridge。

从日志看，媒体链路表现正常：

```text
fs_to_ten: 20ms / 320 bytes 持续转发
ten_to_fs: 20ms / 320 bytes 持续回传
```

本次主要耗时来自：

```text
1. ASR 等 final 结果
2. LLM 首段回复耗时较长
3. TTS 生成首包仍需约 0.78s
4. AI 回复文本较长，完整播放本身约 2.43s
```

其中最明显的优化点是：

```text
ASR final -> LLM 第一段文本
```

本次该段约为 2.28 秒。

## 10. 当前方案与流式方案的差别

当前方案：

```text
ASR final 后才触发 LLM
```

优点：

```text
识别结果稳定
不容易因为 ASR interim 修正导致误答
实现简单
适合早期阶段验证完整链路
```

缺点：

```text
用户停顿后还要等 ASR 判定 final
短句寒暄也会走完整 LLM/TTS 链路
体感延迟较明显
```

行业里常见的优化方式：

```text
1. ASR final 触发
   稳定优先，也是当前方式。

2. ASR interim 预触发
   ASR 出临时文本时提前请求 LLM；final 到来后确认。
   如果 interim 和 final 一致，可以直接继续；如果不一致，需要取消或重来。

3. VAD / endpointing 优化
   更快判断用户是否说完，例如停顿 300-600ms 后触发。
   风险是过早截断长句。

4. 本地短句快速响应
   对“你好”“在吗”“喂”等寒暄直接本地返回固定短句，绕过 LLM。

5. 全双工实时语音模型
   音频直接进入实时模型，模型直接输出音频。
   延迟更低，但供应商绑定、成本和工程复杂度更高。

6. 混合方案
   final 结果兜底，interim 做预热，TTS 首句尽快播，同时支持取消和打断。
```

## 11. 阿里系流式 ASR/TTS 备选方案

后续可以评估将当前 Deepgram ASR 与 ElevenLabs TTS 替换为阿里系 ASR/TTS，作为降低阶段 6A 体感延迟的 A/B 测试方案。

官方能力层面：

```text
阿里百炼实时 ASR:
  支持流式语音识别，适合边说边识别。
  可选模型包括 paraformer-realtime-v2、paraformer-realtime-8k-v2、fun-asr-flash-8k-realtime 等。

阿里 CosyVoice / Qwen TTS:
  支持流式文本输入与流式音频输出。
  适合边生成文本边合成语音，减少 TTS 首包等待时间。
```

项目代码层面，仓库中已经存在可复用的相关扩展：

```text
ASR:
  ai_agents/agents/ten_packages/extension/aliyun_asr_bigmodel_python

TTS:
  ai_agents/agents/ten_packages/extension/cosy_tts_python
  ai_agents/agents/ten_packages/extension/qwen3_tts_python
```

其中 `aliyun_asr_bigmodel_python` 默认模型为：

```text
paraformer-realtime-v2
```

并暴露了以下关键参数：

```text
sample_rate
language_hints
max_sentence_silence
finalize_mode
mute_pkg_duration_ms
```

其中 `max_sentence_silence` 会影响 ASR 判定一句话结束的速度。该值越小，final 越可能提前出现；但过小会增加长句被截断的风险。

`cosy_tts_python` 内部使用 DashScope 的流式合成能力，会通过 `streaming_call(text)` 输入文本，并通过音频回调持续产出 PCM 音频。

但需要注意：**更换模型供应商不等于自动解决 5 秒延迟**。当前延迟由以下几段组成：

```text
用户说完检测
  -> ASR final
  -> LLM 首段文本
  -> TTS 首包音频
  -> 电话侧播放
```

阿里系 ASR/TTS 主要能优化：

```text
ASR final 等待时间
TTS 首包等待时间
```

但不能直接消除：

```text
LLM 首段文本生成耗时
AI 回复语音本身的播放时长
当前 dialog_controller 等 final=True 后才触发 LLM 的策略
```

因此推荐的实施方式不是直接替换主链路，而是新增一条阿里系 A/B 测试 graph：

```text
MicroSIP
  -> FreeSWITCH 9199
  -> Media Hub
  -> sip_media_bridge
  -> aliyun_asr_bigmodel_python
  -> sip_trunk_dialog_controller
  -> DeepSeek/OpenAI-compatible LLM
  -> cosy_tts_python
  -> sip_media_bridge
  -> Media Hub
  -> FreeSWITCH
  -> MicroSIP
```

A/B 测试时先保持 LLM、dialog_controller、FreeSWITCH、Media Hub、MicroSIP 不变，只替换 ASR/TTS，这样才能客观判断供应商切换带来的收益。

建议对比指标：

```text
电话音频进入 -> ASR interim
电话音频进入 -> ASR final
ASR final -> LLM 第一段文本
LLM 第一段文本 -> TTS 首包音频
TTS 首包音频 -> Media Hub 第一包 ten_to_fs
电话音频进入 -> 电话侧听到第一声 AI
```

关键风险点：

```text
1. 当前电话链路是 8000Hz PCM。
2. aliyun_asr_bigmodel_python 默认 sample_rate=16000。
3. 如果使用 16k ASR 模型但实际送入 8k PCM，会导致识别质量和延迟测试不可靠。
4. 优先确认是否使用阿里 8k 实时 ASR 模型，或在进入 ASR 前增加 8k -> 16k 上采样。
5. TTS 下行问题较小，因为 sip_media_bridge 已有下行重采样护栏，可以把非 8k TTS PCM 转为电话侧需要的 8000Hz / 20ms / 320 bytes。
```

推荐后续顺序：

```text
1. 新增阿里系 A/B graph，不替换现有 graph。
2. 配置阿里 ASR 为中文识别，并优先验证 8k 电话音频模型。
3. 配置 CosyVoice TTS，并确认输出采样率。
4. 使用同一句测试语料复测，例如“你好呀”。
5. 和阶段 6A 当前 Deepgram + ElevenLabs 链路做指标对比。
6. 如果收益明确，再考虑将阿里系链路作为后续默认方案。
```

## 12. 建议的下一步优化顺序

建议先做低风险优化，再做架构级优化：

```text
1. 缩短系统提示词与回复长度
   目标：减少 LLM 生成时间和 TTS 播放时长。

2. 降低 LLM max_tokens
   当前 max_tokens=256，对电话短回复偏大。

3. 对寒暄类短句做本地固定响应
   目标：绕过 LLM，验证最低体感延迟。

4. 增加 ASR interim 预触发
   目标：在 final 前提前启动 LLM。
   需要支持取消、去重、final 校验。

5. 增加 AI 播放期间的上行识别护栏
   当前 AI 播放期间 ASR 仍会收到一些空识别结果。
   后续需要避免 AI 自己的下行声音或环境回声影响下一轮对话。

6. 评估更低延迟 TTS 或实时语音模型
   目标：进一步降低 TTS 首包时间和级联链路整体延迟。
```

## 13. 对后续同事的注意事项

1. 分析延迟时不要只看用户体感，要拆成 ASR、LLM、TTS、媒体回传几个阶段。
2. Media Hub 看到 `20ms / 320 bytes` 连续转发，通常说明本地 PCM 媒体链路是健康的。
3. 本地 IP 会随网络变化而变化，MicroSIP 和 FreeSWITCH 的本地配置需要同步更新。
4. 容器日志时间是 UTC，和 Windows 本地时间相差 8 小时。
5. 现阶段 graph 可验证完整 AI 链路，但还不是生产级电话机器人体验。
6. 如果要做 ASR interim 预触发，必须同时设计取消、去重和打断策略，不能只把 `final=False` 文本直接送给 LLM。
7. 如果评估阿里系 ASR/TTS，必须先确认电话 8k 音频和 ASR 采样率匹配，避免因为采样率错误得出错误结论。
