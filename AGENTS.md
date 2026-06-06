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
- `.env` 可能带 UTF-8 BOM；读取本地环境文件时要兼容 `utf-8-sig`，但不要输出或提交真实密钥。
- 线上 `/opt/recov_ten` 可能不是 git 工作树；部署线上版本时必须保留线上 `.env`、systemd 配置和公网 FreeSWITCH 参数，不要用本地目录原样覆盖线上环境。
- 部署记录、线上验收清单等文档可以单独提交为 `docs:` commit；本机 LAN IP、临时测试草稿、未确认的架构方案不要混进业务提交。

## 本地软电话与音频排查

- Mac + Docker + Linphone 本地测试环境下，不要随意打开 FreeSWITCH `disable-rtp-auto-adjust=true`；它可能导致 RTP 回包地址不自动修正，表现为电话接通但无声、无回声或 AI 听不到用户。
- 排查软电话接通无声或 AI 无反应时，优先验证 `9196 echo` 是否有回声，再测真实 AI 外呼；电话能接通不代表 RTP 双向音频一定正常。
- 音频问题优先按链路排查：Linphone 注册状态、Linphone 输入/输出设备、macOS 麦克风权限、FreeSWITCH profile/RTP、网关 inbound RMS 诊断、模型 turn/transcript。
- 网关 inbound RMS 诊断应通过 `inbound_rms_diagnostics_enabled` 开关控制，商用默认关闭；只有排查接通无声、AI 听不到用户或本地 RTP 问题时临时打开。
- `uuid_record` 在 echo 或本地 RTP 转发场景下可能录到静音，不能单独作为“用户没有声音”的核心证据；更可靠的证据是 `9196 echo`、开启诊断后的网关 inbound RMS、FreeSWITCH 通道状态和真实 AI turn/transcript。

## 转人工与 WebRTC 坐席注意事项

- 转人工方案优先查阅 `docs/property-fee-human-handoff-technical-design.md`；WebRTC 坐席接入方案对比优先查阅 `docs/webrtc-seat-sip-wss-vs-verto.md`；线上最终验收按 `docs/property-fee-human-handoff-online-validation-checklist.md` 执行。
- 当前 Python `call_id` 用作客户侧 FreeSWITCH 通道 UUID；桥接客户线路前必须确认它仍对应真实客户通道。
- `agent_uuid` 表示人工坐席侧 FreeSWITCH 通道 UUID，应由后端生成并作为 `origination_uuid` 传给 FreeSWITCH；不要把它当业务单号、坐席账号或 `call_record` 主键。
- 网页坐席接通但无声时，优先检查浏览器麦克风权限、FreeSWITCH RTP 端口范围、Docker UDP 映射、防火墙、STUN/TURN 和 `9196 echo`，不要只凭 SIP 注册或接通状态判断音频链路正常。
- 转人工线上验收必须分层记录：页面可访问、WebRTC 注册、bridge 成功、双路 WAV、ASR turns、`call_record/sys_oss/recording_oss_id`、flow callback SUCCESS 是不同层级，不能把“坐席能通话”当成完整业务闭环成功。
- 人工阶段 ASR 当前商用主线优先使用双 mono：`customer.wav` 只录客户侧，`agent.wav` 只录坐席侧；stereo 合并只作为后续成本优化实验，不作为当前准确率优先的主验收路径。
- 当前转人工失败策略采用方案 A：客户触发转人工后先播“正在为您转接人工座席，请稍等”；无人接听或超时后播“人工座席繁忙”提示并结束通话。`HANDOFF_WAIT_TIMEOUT_SECONDS` 可用于测试和生产等待时间调整。

## 物业费催收提示词边界

- 数据库催收策略、客户画像策略和客服语气配置优先决定沟通方式、推进节奏和语气风格；但不得突破身份核实、隐私保护、勿扰终止、支付安全、事实边界和法律红线。
- 滞纳金、违约金减免、分期、延期和部分缴纳属于业务授权类规则；不要在公共提示词里写成通用减免许可。本轮数据库策略明确给出条件和范围时，AI 只能按该范围说明，并提示具体以物业公司核实和办理为准；未明确授权时不得自行承诺减免、结清、销账或批准方案。
- 客户明确要求勿扰、拒绝继续沟通或强烈反感时，应记录并礼貌结束，不再追问原因、付款、回拨时间或费用安排。
- 工资日、收入情况等个人财务信息遵循最小必要原则；优先问“哪天方便处理”，只有客户主动提到等工资或发工资时，才轻问发薪后哪天方便处理。
- 法务/律师角色必须有系统明确身份和委托关系支撑；不得冒用律师、律师事务所、公检法或司法机关身份，不得制造“马上起诉、马上执行、影响征信”等司法压力。

## Git 约定

- Commit message 使用 Conventional Commits，例如 `feat: 增加实时语音网关`、`fix: 修复播放尾音丢失`、`chore: 提取独立网关项目`。
- 提交前按意图拆分 commit，功能、文档、工具清理分开。
- 不使用 `--no-verify`。
- 不修改仓库级或全局 `user.name` / `user.email`。
- 不添加 AI 工具署名或 `Co-Authored-By`。
