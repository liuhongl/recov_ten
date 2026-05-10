# AI Agent Instructions

## 项目边界

当前仓库已经切到独立 SIP 实时语音网关方向，核心代码只在
`sip-realtime-voice-gateway/` 下。旧 TEN 框架目录不再作为本项目实现依赖，
不要再引用 `ai_agents/`、`core/`、`packages/`、`build/`、`third_party/` 等旧目录。

## 工作区域

- `sip-realtime-voice-gateway/app/`：网关主逻辑，负责 FreeSWITCH 音频接入、端到端实时语音模型连接、播放控制和打断控制。
- `sip-realtime-voice-gateway/configs/`：本地配置模板，真实密钥只放在本地 `.env` 或运行环境变量里。
- `sip-realtime-voice-gateway/freeswitch-local/`：本地 9199 软电话验证用 FreeSWITCH Docker 运行时。
- `sip-realtime-voice-gateway/tests/`：网关单元测试。
- `SIP实时语音网关新项目方案.md`：新项目交接方案、业务链路和阶段计划。
- `TEN电话接入学习笔记.md`：历史调研和电话链路知识笔记。

## 开发约定

- 用中文沟通业务和实现结论。
- 优先尊重事实。如果测试、日志或代码与预期不一致，以证据为准。
- 不提交真实密钥、`.env`、运行日志、音频样本、TLS 证书或本地 IDE 文件。
- 修改后至少运行网关相关测试；涉及 FreeSWITCH 本地运行时时，额外验证 Docker Compose 配置。

## Git 约定

- Commit message 使用 Conventional Commits，例如 `feat: 增加实时语音网关`、`fix: 修复播放尾音丢失`、`chore: 提取独立网关项目`。
- 不使用 `--no-verify`。
- 不修改仓库级或全局 `user.name` / `user.email`。
- 不添加 AI 工具署名或 `Co-Authored-By`。
