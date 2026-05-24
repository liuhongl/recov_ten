# AI Agent Instructions

## 项目边界

当前仓库根目录就是独立 SIP 实时语音网关项目。不要再假设外层还有项目父目录，也不要引用旧 TEN 框架目录。

## 工作区域

- `app/`: 网关主逻辑，负责 FreeSWITCH 音频接入、端到端实时语音模型连接、播放控制和打断控制。
- `configs/`: 本地配置模板，真实密钥只放在本地 `.env` 或运行环境变量里。
- `freeswitch-local/`: 本地 9199 软电话验证用 FreeSWITCH Docker 运行时。
- `tests/`: 网关单元测试。
- `static/pages/handoff.html`: 当前交接总入口，包含业务链路、实现状态、数据格式、测试环境和后续事项。
- `static/pages/notes.html`: 历史调研和电话链路知识笔记。
- `static/pages/mac-softphone.html`: Mac 软电话接入 9199 本地测试指导。
- `static/pages/agent-readme.html`: 推荐的 AI / Agent 协作方式。需要沉淀长期工作规则时，优先从这里同步到本文档。

## 开发约定

- 用中文沟通业务和实现结论。
- 优先尊重事实。如果测试、日志或代码与预期不一致，以证据为准。
- 不提交真实密钥、`.env`、运行日志、音频样本、TLS 证书或本地 IDE 文件。
- 修改后至少运行网关相关测试；涉及 FreeSWITCH 本地运行时时，额外验证 Docker Compose 配置。
- `freeswitch-local/conf/vars.xml` 可能包含本机 LAN IP，只能视为本地软电话测试配置；部署或提交前必须确认不会用它覆盖公网服务器 SIP/RTP 配置。
- 涉及数据库、外呼控制、`/outbound-test` 或实时媒体结果落库时，优先运行 `uv run --with pytest pytest -q`。

## Git 约定

- Commit message 使用 Conventional Commits，例如 `feat: 增加实时语音网关`、`fix: 修复播放尾音丢失`、`chore: 提取独立网关项目`。
- 提交前按意图拆分 commit，功能、文档、工具清理分开。
- 不使用 `--no-verify`。
- 不修改仓库级或全局 `user.name` / `user.email`。
- 不添加 AI 工具署名或 `Co-Authored-By`。
