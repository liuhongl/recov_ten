# SIP 实时语音网关

本仓库当前聚焦独立的 `sip-realtime-voice-gateway` 项目，用于把电话侧
FreeSWITCH/SIP/RTP 音频接入端到端实时语音模型，实现低延迟 AI 外呼/通话场景。

旧 TEN 框架源码已从当前清理分支移除。后续开发、测试和交接应以
`sip-realtime-voice-gateway/` 为准。

## 主要目录

- `sip-realtime-voice-gateway/app/`：网关主代码。
- `sip-realtime-voice-gateway/configs/`：配置模板。
- `sip-realtime-voice-gateway/freeswitch-local/`：本地 FreeSWITCH 软电话测试环境。
- `sip-realtime-voice-gateway/tests/`：自动化测试。
- `SIP实时语音网关新项目方案.md`：业务链路、阶段计划、数据格式和交接说明。
- `TEN电话接入学习笔记.md`：历史调研笔记。

## 快速验证

```powershell
cd sip-realtime-voice-gateway
python -m pytest
python -m app.main --config configs/local.example.toml --check-config
cd freeswitch-local
docker compose config
```

真实通话测试需要本地 FreeSWITCH 容器、软电话和本地 `.env` 中的模型服务密钥。
`.env`、运行日志、音频文件和 TLS 证书不允许提交。
