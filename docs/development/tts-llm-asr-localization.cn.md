# TTS / LLM / ASR 国产化技术方案：阿里百炼统一 Key 版本

> 目标：在不改 TEN runtime 的前提下，用阿里百炼 / DashScope API Key 统一接入 ASR、LLM、TTS，先跑通云端低风险闭环，再为后期本地化语音模型替换保留接口边界。

## 1. 结论

当前最优方案：

```text
ASR: aliyun_asr_bigmodel_python
LLM: openai_llm2_python + 百炼 OpenAI-compatible endpoint
TTS: cosy_tts_python
Graph: voice_assistant_cn_aliyun
```

这套方案的核心价值：

1. **同一个百炼 API Key 可覆盖 ASR、LLM、TTS**，密钥治理更简单。
2. **改动集中在 graph 和 `.env`**，不需要改 TEN runtime。
3. **后期本地化替换路径清晰**，本地 ASR/TTS 只需实现 TEN 标准扩展接口。
4. **比传统阿里 NLS AccessKey 方案更统一**，避免同时维护 `AppKey + AKID + AKSecret` 和 DashScope Key 两套鉴权体系。

## 2. 当前项目事实

当前 `voice-assistant` 示例默认链路为：

| 模块 | 当前默认扩展 | 默认供应商 |
| --- | --- | --- |
| ASR | `deepgram_asr_python` | Deepgram |
| LLM | `openai_llm2_python` | OpenAI |
| TTS | `elevenlabs_tts2_python` | ElevenLabs |

项目中已经存在阿里相关扩展：

| 能力 | 扩展 | 鉴权方式 | 建议状态 |
| --- | --- | --- | --- |
| ASR | `aliyun_asr_bigmodel_python` | 百炼 / DashScope API Key | 推荐 |
| ASR | `aliyun_asr` | 传统 NLS `appkey + akid + aksecret` | 备选 |
| TTS | `cosy_tts_python` | 百炼 / DashScope API Key | 推荐 |
| LLM | `openai_llm2_python` | OpenAI-compatible API Key + base_url | 推荐 |

注意：如果目标是“ASR、LLM、TTS 都用阿里百炼 Key”，ASR 必须选 `aliyun_asr_bigmodel_python`，不是 `aliyun_asr`。

## 3. 推荐架构

```text
浏览器 / 客户端
  -> Agora RTC
  -> agora_rtc
  -> aliyun_asr_bigmodel_python
  -> main_control
  -> openai_llm2_python
  -> main_control
  -> cosy_tts_python
  -> agora_rtc
  -> 播放语音
```

TEN 的模块接口保持不变：

| 模块 | TEN 输入 | TEN 输出 |
| --- | --- | --- |
| ASR | `pcm_frame` | `asr_result` |
| LLM | `text_data` / `chat_completion_call` | `text_data` |
| TTS | `tts_text_input` | `pcm_frame`、`tts_audio_start`、`tts_audio_end` |

后期本地化时，只替换 ASR/TTS node 的 addon，不改主流程。

## 4. `.env` 配置

建议使用新的百炼 Key，不要继续使用已经泄露过的旧 key。

```bash
# ---- ASR: 阿里百炼 Paraformer 实时语音识别 ----
ALIYUN_ASR_BIGMODEL_API_KEY=your_new_bailian_api_key

# ---- LLM: 阿里百炼通义千问 OpenAI 兼容接口 ----
LLM_API_KEY=your_new_bailian_api_key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# ---- TTS: 阿里百炼 CosyVoice 语音合成 ----
COSY_TTS_API_KEY=your_new_bailian_api_key
COSY_TTS_MODEL=cosyvoice-v3
COSY_TTS_VOICE=loongluna_v2

# ---- TEN 服务 ----
SERVER_PORT=8080
LOG_STDOUT=true
WORKERS_MAX=100
WORKER_QUIT_TIMEOUT_SECONDS=60
```

说明：

1. `LLM_API_KEY`、`COSY_TTS_API_KEY`、`ALIYUN_ASR_BIGMODEL_API_KEY` 可以填写同一个百炼 API Key。
2. 不要把真实 key 写入 `property.json`、README、提交记录或聊天记录。
3. 修改 `.env` 后必须重启容器，因为项目文档明确说明 `.env` 只在容器启动时加载。
4. 你之前的 `TEN_MEDIA_PORT`、`BRIDGE_PORT` 不是当前 TEN 文档里的标准变量；除非你有自定义服务读取它们，否则不会影响 `voice-assistant` 主链路。

## 5. Graph 配置建议

新增 graph：`voice_assistant_cn_aliyun`。

### 5.1 ASR Node

```json
{
  "type": "extension",
  "name": "stt",
  "addon": "aliyun_asr_bigmodel_python",
  "extension_group": "stt",
  "property": {
    "params": {
      "api_key": "${env:ALIYUN_ASR_BIGMODEL_API_KEY}",
      "model": "paraformer-realtime-v2",
      "sample_rate": 16000,
      "language_hints": ["zh", "en"],
      "punctuation_prediction_enabled": true,
      "inverse_text_normalization_enabled": true,
      "max_sentence_silence": 600
    }
  }
}
```

建议：

1. 中文语音助手默认 `language_hints` 使用 `["zh", "en"]`，兼容中英混说。
2. `max_sentence_silence` 不宜过大，否则用户停顿后响应慢；初始建议 600 ms，再实测调整。
3. 如果要提高专有名词识别率，后续再增加热词表，不要第一版就复杂化。

### 5.2 LLM Node

```json
{
  "type": "extension",
  "name": "llm",
  "addon": "openai_llm2_python",
  "extension_group": "chatgpt",
  "property": {
    "base_url": "${env:LLM_BASE_URL}",
    "api_key": "${env:LLM_API_KEY}",
    "model": "${env:LLM_MODEL}",
    "max_tokens": 512,
    "frequency_penalty": 0.3,
    "prompt": "你是一个中文实时语音助手。回答要自然、简洁、口语化，避免 Markdown、表格和过长枚举。",
    "greeting": "你好，我已连接。有什么可以帮你？",
    "max_memory_length": 10
  }
}
```

建议：

1. 默认先用 `qwen-plus`，综合延迟、质量和成本更均衡。
2. 如果追求更低成本或更低延迟，可以评估 `qwen-turbo` / `qwen-flash`。
3. DeepSeek key 先作为备用 graph 使用，不建议第一版混在同一个 graph 里。

### 5.3 TTS Node

```json
{
  "type": "extension",
  "name": "tts",
  "addon": "cosy_tts_python",
  "extension_group": "tts",
  "property": {
    "dump": false,
    "dump_path": "./",
    "params": {
      "api_key": "${env:COSY_TTS_API_KEY}",
      "model": "${env:COSY_TTS_MODEL|cosyvoice-v3}",
      "sample_rate": 16000,
      "voice": "${env:COSY_TTS_VOICE|loongluna_v2}"
    }
  }
}
```

建议：

1. `sample_rate` 先用 16000，和 RTC/ASR 链路对齐，减少重采样风险。
2. 音色先用稳定官方音色，跑通后再做音色主观评测。
3. 关闭 `dump`，避免产生音频文件和隐私风险；排查问题时再临时开启。

## 6. 传统阿里 NLS 配置不再作为主方案

你之前的配置形态：

```bash
ALIYUN_ASR_APPKEY=...
ALIYUN_ASR_AKID=...
ALIYUN_ASR_AKSECRET=...
```

这是传统阿里 NLS / AccessKey 体系，对应项目扩展 `aliyun_asr`。

如果继续使用 `aliyun_asr`，项目实际读取变量名是：

```bash
ALIYUN_ASR_APPKEY=...
ALIYUN_ASR_AKID=...
ALIYUN_ASR_AKKEY=...
```

注意不是 `ALIYUN_ASR_AKSECRET`。但在本方案中，它只作为备用，不作为推荐主链路。

## 7. DeepSeek 备用方案

DeepSeek key 建议作为备用 graph，例如：

```text
voice_assistant_cn_deepseek
  stt: aliyun_asr_bigmodel_python
  llm: openai_llm2_python + DeepSeek base_url/model
  tts: cosy_tts_python
```

对应 `.env`：

```bash
DEEPSEEK_API_KEY=your_deepseek_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

不建议第一版就把百炼和 DeepSeek 做运行时自动切换。先用独立 graph 做 A/B 对比，记录首 token 延迟、回答质量、失败率，再决定是否做路由层。

## 8. 集成步骤

1. 在 `ai_agents/.env` 写入新的百炼 Key。
2. 在 `ai_agents/agents/examples/voice-assistant/tenapp/property.json` 复制现有 `voice_assistant` graph。
3. 新 graph 命名为 `voice_assistant_cn_aliyun`。
4. 保留 `agora_rtc`、`main_control`、`message_collector`、`weatherapi_tool_python`、`streamid_adapter` 和原 connections。
5. 只替换 `stt`、`llm`、`tts` 三个 node。
6. 确认 `ai_agents/agents/examples/voice-assistant/tenapp/manifest.json` 已包含：

```json
{"path": "../../../ten_packages/extension/aliyun_asr_bigmodel_python"}
{"path": "../../../ten_packages/extension/openai_llm2_python"}
{"path": "../../../ten_packages/extension/cosy_tts_python"}
```

当前项目已经包含这些依赖，不需要新增 manifest 依赖。

## 9. 启动与重启

修改 `.env` 后：

```bash
cd ai_agents
docker compose down
docker compose up -d
```

容器重启后安装依赖并启动：

```bash
docker exec ten_agent_dev bash -c "cd /app/agents/examples/voice-assistant/tenapp && bash scripts/install_python_deps.sh"
docker exec ten_agent_dev bash -c "cd /app/agents/examples/voice-assistant && task install"
docker exec -d ten_agent_dev bash -c "cd /app/agents/examples/voice-assistant && task run > /tmp/task_run.log 2>&1"
```

如果只是新增 graph，也需要完整重启 API server、Playground 和 TMAN Designer，否则前端可能仍使用旧 graph 缓存。

## 10. 验证命令

健康检查：

```bash
curl -s http://localhost:8080/health
```

确认 graph 可见：

```bash
curl -s http://localhost:8080/graphs | jq -r '.data[].name'
```

预期包含：

```text
voice_assistant_cn_aliyun
```

启动会话时使用：

```json
{
  "request_id": "test-cn-aliyun",
  "channel_name": "test_cn_aliyun",
  "user_uid": 176573,
  "graph_name": "voice_assistant_cn_aliyun",
  "timeout": 60
}
```

日志排查：

```bash
docker exec ten_agent_dev bash -c "strings /tmp/task_run.log | tail -n 120"
```

## 11. 验收标准

| 类别 | 验收项 | 通过标准 |
| --- | --- | --- |
| 配置 | graph 可见 | `/graphs` 返回 `voice_assistant_cn_aliyun` |
| ASR | 中文识别 | 普通话输入可输出 `asr_result.final=true` |
| ASR | 中英混说 | 简单中英混合词可识别 |
| LLM | 中文回复 | 回复自然、简洁、无明显英文默认提示 |
| TTS | 语音输出 | 客户端可听到 CosyVoice 合成语音 |
| 打断 | 用户插话 | 旧 TTS 能停止，不继续播长句 |
| 错误 | key 错误 | 日志能定位鉴权错误，但不泄露原始 key |
| 性能 | 首句可听 | 目标小于 3 秒，按真实网络实测调整 |

## 12. 后期本地化路线

不要把“本地化”理解成一次性重写。正确路线是先保留 TEN 接口，逐段替换。

### 12.1 ASR 本地化

目标扩展：

```text
local_asr_python
```

需要实现：

| 方法 | 目的 |
| --- | --- |
| `start_connection()` | 启动本地 ASR 服务连接 |
| `send_audio()` | 发送 PCM 音频帧 |
| `finalize()` | 结束一句话并输出最终识别 |
| `send_asr_result()` | 按 TEN 标准输出结果 |

本地模型候选：

```text
FunASR / Paraformer / Whisper 派生流式模型
```

第一阶段不要直接替换，先用阿里百炼 ASR 建立识别准确率和延迟基线。

### 12.2 TTS 本地化

目标扩展：

```text
local_tts_python
```

需要实现：

| 方法 | 目的 |
| --- | --- |
| `request_tts()` | 接收文本并请求本地 TTS |
| `cancel_tts()` | 支持打断 |
| `send_tts_audio_data()` | 输出 PCM 音频 |
| `send_tts_audio_start/end()` | 输出播放状态 |

本地模型候选：

```text
CosyVoice / Qwen3-TTS / Fish Audio / 其他可商用语音模型
```

本仓库已有 `qwen3_tts_python`，但默认使用 `cuda:0` 和本地模型，适合后期验证，不适合作为第一阶段默认方案。

### 12.3 LLM 本地化

LLM 最好继续保持 OpenAI-compatible 接口：

```text
vLLM / SGLang / Ollama / 企业内网大模型网关
```

只要本地服务暴露：

```text
POST /v1/chat/completions
```

就可以继续复用 `openai_llm2_python`，只替换：

```bash
LLM_BASE_URL=http://your-local-llm/v1
LLM_API_KEY=local_or_dummy_key
LLM_MODEL=your-local-model
```

## 13. 安全要求

1. 已经贴出过的 key 必须轮换，不能继续用于生产。
2. `.env` 不提交，不截图，不发聊天。
3. 所有 graph 使用 `${env:VAR_NAME}` 引用密钥。
4. 排查日志时只看脱敏后的配置，不输出完整请求头。
5. 音频 dump 默认关闭，除非定位问题。
6. 如果后期本地模型落地，需要补充模型许可证、音色授权、数据留存和访问审计。

## 14. 最终推荐

现在执行：

```text
voice_assistant_cn_aliyun
  ASR: aliyun_asr_bigmodel_python
  LLM: openai_llm2_python + qwen-plus
  TTS: cosy_tts_python
```

后续扩展：

```text
voice_assistant_cn_deepseek
  ASR: aliyun_asr_bigmodel_python
  LLM: openai_llm2_python + deepseek-chat
  TTS: cosy_tts_python

voice_assistant_local_speech
  ASR: local_asr_python
  LLM: openai_llm2_python + 本地 OpenAI-compatible 网关
  TTS: local_tts_python
```

结论：先用阿里百炼统一 Key 方案建立稳定基线，再逐段替换本地语音模型，是当前风险最低、迁移成本最低、验证路径最清楚的方案。
