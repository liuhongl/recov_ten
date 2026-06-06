# 111.229.146.182 当前版本部署前检查

本文档用于把当前本地版本部署到 `111.229.146.182` 做真实电话单通验证。目标是受控测试，不是生产上线或批量外呼。

## 2026-06-05 发布范围核查记录

核查时本地分支：

```text
codex/fix/realtime-dialog-context
```

核查命令：

```bash
git status --short --branch
git log --oneline --reverse origin/codex/fix/realtime-dialog-context..HEAD
git diff --name-status origin/codex/fix/realtime-dialog-context..HEAD
```

核查结论：

```text
1. 核查时分支相对 origin/codex/fix/realtime-dialog-context ahead 78 个 commit。
2. 这 78 个 commit 不是单独的火山文件 ASR 小改，而是一组完整发布包。
3. 发布包覆盖外呼落库、flow callback、物业费提示词边界、浏览器 Prompt Lab、WebRTC 转人工、人工转写、完整录音上传、ASR adapter、测试页和相关测试。
4. 本地仍有 `freeswitch-local/conf/vars.xml` 未提交改动，它属于本机 LAN IP 配置，不能同步覆盖线上。
5. `docs/property-fee-human-handoff-online-validation-checklist.md` 是 2026-06-05 新增的线上分层验收补充，需要随本轮文档同步。
```

注意：本节是 2026-06-05 线上部署前的历史核查记录，不代表后续提交或 push 后的实时分支状态；实时状态以当前 `git status --short --branch` 为准。

本轮发布代码范围按模块理解：

```text
外呼与业务结果：
- app/call_control.py
- app/postgres.py
- app/flow_callback.py
- app/health_server.py
- app/main.py

实时语音与转人工：
- app/realtime_phone_gateway.py
- app/doubao_s2s_realtime.py
- app/handoff_transcript.py
- app/handoff_asr_adapter.py

录音与 OSS：
- app/recording_upload.py
- docs/sql/2026-06-03-call-recording-upload.sql
- docs/call-recording-upload-runbook.md

配置与环境：
- app/config.py
- app/env_loader.py
- configs/local.example.toml
- .env.example

页面与静态资源：
- static/outbound-test.html
- static/webrtc-agent-test.html
- static/browser-realtime-test.html
- static/vendor/jssip.min.js
- static/vendor/jssip.LICENSE

FreeSWITCH 本轮相关文件：
- freeswitch-local/conf/sip_profiles/internal.xml
- freeswitch-local/docker-compose.yml
- freeswitch-local/scripts/sip_realtime_audio_stream_start.lua

明确排除：
- freeswitch-local/conf/vars.xml
```

注意：下方 `2026-05-24` 的“本次必须同步的代码文件”是当时外呼落库阶段的历史范围，不代表 2026-06-05 转人工、录音上传和 ASR adapter 的完整发布范围。

## 2026-06-05 线上只读预检结果

连接信息：

```text
host=111.229.146.182
user=root
time=2026-06-05 15:52 CST
```

只读检查结论：

```text
1. `/opt/recov_ten` 存在。
2. `recov-ten-gateway.service` 当前 active / enabled。
3. 当前 Python 进程：
   /opt/recov_ten/.venv/bin/python -m app.main --config configs/local.example.toml --env-file .env --media-mode realtime
4. `/opt/recov_ten` 当前看起来不是 git 工作树，不能用 git pull 直接更新。
5. FreeSWITCH 容器 `sip_realtime_freeswitch` 正在运行。
6. Docker 已映射 5066/tcp、5080/tcp+udp、5089/udp、16384-16484/udp、18021/tcp。
7. external profile 显示 Ext-SIP-IP / Ext-RTP-IP 都是 `111.229.146.182`，external_sip_port 是 `5080`。
8. `sofia status gateway sip-provider` 返回 `Invalid Gateway!`。
9. `freeswitch-local/conf/sip_profiles/external/` 里只有 `sip-provider.xml.template` 和 `sip-provider-sandbox.xml`，没有实际加载的 `sip-provider.xml`。
```

当前 `/ready` 关键运行态：

```text
server.host=0.0.0.0
server.port=9100
outbound.enabled=true
outbound.endpoint_template=sofia/gateway/sip-provider/{destination}
outbound.event_socket_enabled=true
features.recording_enabled=true
features.recording_dir=/var/lib/freeswitch/recordings
features.inbound_rms_diagnostics_enabled=true
human_transcript.enabled=true
human_transcript.provider=mock
human_transcript.timeout_seconds=30
flow_callback.enabled=true
flow_callback.http.enabled=true
flow_callback.http.base_url=http://127.0.0.1:19090
```

当前 `.env` 已发现的相关 key：

```text
OUTBOUND_ENDPOINT_TEMPLATE
POSTGRES_DSN
POSTGRES_ENABLED
FLOW_CALLBACK_ENABLED
RECORDING_ENABLED
RECORDING_DIR
HUMAN_TRANSCRIPT_ENABLED
HUMAN_TRANSCRIPT_HTTP_URL
```

当前 `.env` 未发现但 2026-06-05 完整验收需要补齐或确认的 key：

```text
HUMAN_TRANSCRIPT_PROVIDER
HUMAN_TRANSCRIPT_TIMEOUT_SECONDS
RECORDING_HOST_DIR
CALL_RECORDING_ENABLED
CALL_RECORDING_UPLOAD_ENABLED
CALL_RECORDING_DIR
CALL_RECORDING_HOST_DIR
CALL_RECORDING_OBJECT_PREFIX
CALL_RECORDING_UPLOAD_TIMEOUT_SECONDS
DOUBAO_FILE_ASR_API_KEY
DOUBAO_FILE_ASR_RESOURCE_ID
```

阻断判断：

```text
1. 现在不能直接做真实 sip-provider 出局测试，因为 `sip-provider` gateway 未加载。
2. 现在不能做火山文件 ASR 完整验收，因为 `/ready` 显示 human_transcript.provider=mock，不是 http_json / file ASR adapter。
3. 现在不能验完整录音上传到 MinIO/sys_oss，因为 `CALL_RECORDING_*` 上传配置未发现。
4. 可以先部署代码和补配置，再做受控单路测试。
```

2026-06-05 推荐执行顺序：

```text
1. 备份 `/opt/recov_ten`。
2. 同步本地当前发布包到 `/opt/recov_ten`，保留线上 `.env` 和 `freeswitch-local/conf/vars.xml`。
3. 复制或生成 `freeswitch-local/conf/sip_profiles/external/sip-provider.xml`，再 reload FreeSWITCH external profile。
4. 补齐线上 `.env`：
   - `HUMAN_TRANSCRIPT_PROVIDER=http_json`
   - `HUMAN_TRANSCRIPT_HTTP_URL=http://127.0.0.1:9200/handoff-transcript`
   - `HUMAN_TRANSCRIPT_TIMEOUT_SECONDS=180`
   - `RECORDING_HOST_DIR=/opt/recov_ten/recordings/handoff`
   - `CALL_RECORDING_*`
   - `DOUBAO_FILE_ASR_*`
5. 创建宿主机录音目录并确认 Docker 挂载：
   - 完整录音：`/opt/recov_ten/recordings`
   - 人工双路临时录音：`/opt/recov_ten/recordings/handoff`
6. 启动或托管 `app.handoff_asr_adapter`，监听 `127.0.0.1:9200`。
7. 重启 `recov-ten-gateway.service`。
8. 检查 `/ready`、`sofia status gateway sip-provider`、9200 adapter。
9. 第一通只打白名单人工测试号码。
```

## 2026-06-05 线上环境支持矩阵

本节用于回答：`111.229.146.182` 当前是否支持“先上准线上/线上测试环境，不做批量外呼，只允许白名单测试号码，先单通拨测”。

| 检查项 | 当前结论 | 证据 | 下一步 |
|---|---|---|---|
| 当前代码版本 | 不满足 2026-06-05 发布包 | 线上缺 `app/handoff_asr_adapter.py`、`app/recording_upload.py`、`docs/call-recording-upload-runbook.md`；`app/main.py` mtime 为 2026-06-02 | 必须先同步本地发布包，保留线上 `.env` 和 `vars.xml` |
| 线上网关服务 | 支持 | `recov-ten-gateway.service=active/enabled`，`GET /ready=200` | 可作为控制面入口 |
| 测试页面 | 支持 | `/outbound-test=200`，`/webrtc-agent-test=200`，`/vendor/jssip.min.js=200` | 页面可用于受控联调 |
| FreeSWITCH 容器 | 支持 | `sip_realtime_freeswitch` healthy，external/internal profiles running | 可继续作为媒体边界 |
| SIP trunk 出局 | 当前不支持真实 `sip-provider` | `sofia status gateway sip-provider => Invalid Gateway!`，当前目录缺 `sip-provider.xml` | 从历史备份恢复曾打通过的 `sip-provider.xml`，reload external profile |
| 真实线路端口 | 部分支持 | external profile `Ext-SIP-IP/Ext-RTP-IP=111.229.146.182`，`5080/tcp+udp` 映射，UDP 5080 轻探测通过 | 真实拨号前恢复 `sip-provider` gateway |
| 浏览器坐席 WS/WSS | 部分支持 | internal profile 有 `WS-BIND-URL :5066` / `WSS-BIND-URL :7443`；外部 TCP 5066/7443 可连；TLS 握手 7443/5066 返回 `unexpected eof` | 当前不满足可信公网 WSS；可先用 HTTP/WS 或 SSH/localhost 方式测试，公网浏览器商用需 HTTPS/WSS 证书或反代 |
| RTP 端口 | 部分支持 | Docker 映射 `16384-16484/udp`，OS firewalld inactive，UDP 首尾端口轻探测通过 | 云安全组无法仅靠主机命令完全确认，最终仍要用真实通话验证双向 RTP |
| TURN/STUN | 只支持 STUN 默认值 | 坐席页默认 `stun:stun.l.google.com:19302,stun:stun.cloudflare.com:3478`，未发现 TURN 配置 | 外网复杂 NAT 下需补 TURN |
| 人工双路录音目录 | 当前不满足宿主机读取 | 容器内 `/var/lib/freeswitch/recordings` 有历史 `customer.wav/agent.wav`，但 Docker 未挂载 `/opt/recov_ten/recordings`，宿主机目录不存在 | 增加 recordings 挂载，并设置 `RECORDING_HOST_DIR` |
| 完整通话录音上传 | 数据库支持，运行配置不足 | DB 有 `call_record.recording_oss_id`、`sys_oss_config`、`sys_oss`，且有活跃 MinIO 配置；但线上 `.env` 未发现 `CALL_RECORDING_*` | 补 `CALL_RECORDING_*` 和 Docker 录音挂载后再验上传 |
| 火山文件 ASR | 当前不支持 | 线上 `.env` 为 `HUMAN_TRANSCRIPT_PROVIDER=mock`，未发现 `DOUBAO_FILE_ASR_*`，9200 adapter 未运行 | 补火山文件 ASR env，启动 `app.handoff_asr_adapter`，改 provider 为 `http_json` |
| MinIO/sys_oss | 支持数据库侧配置 | `sys_oss_config` 有 `status='0'` 活跃配置，endpoint/bucket/access/secret/domain 已填；`sys_oss` 表存在 | 需结合完整录音上传配置做端到端验证 |
| 批量外呼控制 | 需要人工流程约束 | 当前验证目标是单通白名单；未检查批量调度护栏 | 第一轮不要开放批量入口 |

结论：

```text
182 当前支持作为线上受控测试环境的基础控制面和媒体服务器，
但还不支持直接跑“真实 SIP trunk + WebRTC 坐席 + 双路录音 + 火山文件 ASR + MinIO/sys_oss 回填”的完整闭环。

最小修复顺序：
1. 备份 `/opt/recov_ten` 并同步本地 2026-06-05 发布包，排除 `.env` 和 `freeswitch-local/conf/vars.xml`。
2. 恢复并 reload 真实 `sip-provider` gateway。
3. 补 recordings 宿主机挂载和目录。
4. 补火山文件 ASR env 并启动 9200 adapter。
5. 补 `CALL_RECORDING_*` 上传配置。
6. 再做单通白名单拨测。
```

## 2026-06-05 部署准备执行记录

已执行：

```text
1. 备份线上目录：
   /opt/recov_ten_backups/recov_ten.20260605-163820.pre-20260605-sync
2. 用 tar 包同步本地 2026-06-05 发布包到 `/opt/recov_ten`。
3. 同步时保留线上 `.env`、`freeswitch-local/conf/vars.xml`、`freeswitch-local/docker-compose.yml` 和 `sip-provider.xml`。
4. 远端编译检查通过：
   `.venv/bin/python -m compileall -q app tests`
5. 远端导入检查通过：
   `app.handoff_asr_adapter`、`app.recording_upload`、`app.handoff_transcript`
6. 复制 `sip-provider.xml.template` 为 `sip-provider.xml`。
7. 在远端 `docker-compose.yml` 增加录音挂载：
   `/opt/recov_ten/freeswitch-local/recordings -> /var/lib/freeswitch/recordings`
8. 创建并启动 systemd 服务：
   `recov-ten-handoff-asr-adapter.service`
9. 重建 FreeSWITCH 容器以应用录音挂载。
10. 重启 `recov-ten-gateway.service` 以加载新配置。
```

当前运行态检查：

```text
recov-ten-gateway.service = active
recov-ten-handoff-asr-adapter.service = active
sip_realtime_freeswitch = healthy
127.0.0.1:9200 = listening
show calls = 0 total
```

`GET /ready` 当前关键值：

```text
features.recording_enabled=true
features.recording_dir=/var/lib/freeswitch/recordings/handoff
features.recording_host_dir=/opt/recov_ten/freeswitch-local/recordings/handoff
call_recording.enabled=true
call_recording.upload_enabled=true
call_recording.directory=/var/lib/freeswitch/recordings
call_recording.host_directory=/opt/recov_ten/freeswitch-local/recordings
human_transcript.enabled=true
human_transcript.provider=http_json
human_transcript.http_url=http://127.0.0.1:9200/handoff-transcript
human_transcript.timeout_seconds=180
```

FreeSWITCH 检查：

```text
sofia status gateway sip-provider:
State=NOREG
Status=UP
Contact=sip:gw+sip-provider@111.229.146.182:5080;transport=udp;gw=sip-provider
Proxy=sip:47.94.86.132:5089
```

录音挂载检查：

```text
宿主机写入：
/opt/recov_ten/freeswitch-local/recordings/.host-write-test-*

容器内读取：
/var/lib/freeswitch/recordings/.host-write-test-*

结果：ok
```

页面检查：

```text
/outbound-test = 200
/webrtc-agent-test = 200
/vendor/jssip.min.js = 200
/ready = 200
```

注意：

```text
1. 9200 ASR adapter 对 GET /health 和 GET /ready 返回 501 是当前实现行为；
   它是 POST /handoff-transcript 服务，501 只能证明端口上是该 BaseHTTP 服务在响应。
2. 本轮尚未拨真实电话，尚未验证真实 RTP、双路 WAV、火山文件 ASR、MinIO 上传、recording_oss_id 和 callback SUCCESS。
3. TURN 仍未配置；公网坐席 WebRTC 若出现无声或单向音频，需要补 TURN 或先用受控网络/隧道测试。
```

## 2026-06-05 第一通真实线路拨测

拨测目标：先验证普通 AI 外呼真实 SIP trunk，不触发转人工。

请求摘要：

```text
destination=178****6638
external_call_id=real-sip-single-20260605171430
caller_id_number=037123124845
endpoint=sofia/gateway/sip-provider/178****6638
context.scene=real-sip-single-preflight
context.taskId=real-sip-single-20260605171430
```

结果：

```text
POST /calls => 202 Accepted
Python call_id=be089b0b4ac640cbb9af30b92dc0aa2d
status=failed
phase=busy
freeswitch_reply=-ERR CALL_REJECTED
hangup_cause=CALL_REJECTED
sip_status=403
sip_reason=21
ringing_at_ms=null
answered_at_ms=null
media_connected_at_ms=null
turns=[]
recording file=none
show calls=0 total
```

FreeSWITCH gateway 状态：

```text
sip-provider State=NOREG
sip-provider Status=UP
CallsOUT=1
FailedCallsOUT=1
```

分层结论：

```text
1. Python 控制面已受理请求，flow callback ACCEPTED 已发送。
2. FreeSWITCH 真实 `sip-provider` gateway 已被使用。
3. 失败发生在 SIP trunk 出局/上游拒绝阶段。
4. 本通没有进入振铃、接听、RTP 媒体、AI 对话、录音、ASR、MinIO 上传或 recording_oss_id 回填阶段。
```

下一步排查方向：

```text
1. 结合供应商 CDR / SIP trace 查 `403` 和 reason `21` 的具体含义。
2. 确认该被叫号码是否允许测试、是否被运营商/供应商策略拒绝。
3. 确认主叫 `037123124845` 对该号码段是否有显号/外呼权限。
4. 换一个已知此前可接通的白名单号码做对照，不要把本次 `403` 当成媒体或 ASR 问题。
```

## 2026-06-05 第二通真实线路对照拨测

拨测目标：使用历史上曾出现过成功样本的号码做普通 AI 外呼对照。

请求摘要：

```text
destination=185****8743
external_call_id=real-sip-single-20260605171805
caller_id_number=037123124845
endpoint=sofia/gateway/sip-provider/185****8743
context.scene=real-sip-single-preflight
context.taskId=real-sip-single-20260605171805
```

结果：

```text
POST /calls => 202 Accepted
Python call_id=242451ffe8de4ec3a87683b7c9fdb31c
status=failed
phase=busy
freeswitch_reply=-ERR CALL_REJECTED
hangup_cause=CALL_REJECTED
sip_status=403
sip_reason=21
ringing_at_ms=null
answered_at_ms=null
media_connected_at_ms=null
turns=[]
recording file=none
show calls=0 total
```

FreeSWITCH gateway 状态：

```text
sip-provider State=NOREG
sip-provider Status=UP
CallsOUT=2
FailedCallsOUT=2
```

对照结论：

```text
1. 178****6638 和 185****8743 两通均被上游立即 `403 / CALL_REJECTED`。
2. 两通都没有 ringing、answered 或 media_connected。
3. 当前阻断点仍在 SIP trunk 出局/供应商策略阶段。
4. 暂时不能继续验证 RTP、AI 对话、WebRTC 坐席、录音、ASR、MinIO 或 callback SUCCESS。
```

下一步建议：

```text
请线路供应商按以下两条 external_call_id / 时间查 CDR 或 SIP trace：
- real-sip-single-20260605171430，178****6638
- real-sip-single-20260605171805，185****8743

重点确认：
1. `403` 的具体拒绝原因。
2. 主叫 `037123124845` 当前是否仍允许外呼。
3. 两个被叫号码是否被线路、区域、运营商或风控策略拒绝。
4. 供应商当前是否要求调整号码格式，例如 `86` / `+86` / 本地号格式。
```

## 当前结论

可以准备部署当前版本到 `111.229.146.182` 做真实电话测试，但不能把本地目录原样覆盖到服务器。

原因：

- `111.229.146.182` 是历史真实 `sip-provider` 成功样本对应的公网服务器。
- 本地当前版本已完成软电话端到端验证，`call_record` 的 `status / started_at / finished_at / transcript` 更新链路已跑通。
- 当前线上 `/ready` 可访问，且 `OUTBOUND_ENDPOINT_TEMPLATE=sofia/gateway/sip-provider/{destination}` 已是真实线路模板。
- 当前线上 `/outbound-test` 仍是旧页面，没有 `callId` 输入框，说明需要同步当前前端和 Python 更新逻辑。
- 本地 `freeswitch-local/conf/vars.xml` 当前是局域网 IP `192.168.0.107`，不能同步覆盖线上公网配置。

## 本地验证结果

2026-05-24 已执行：

```bash
uv run --with pytest pytest tests/test_postgres.py tests/test_call_control.py tests/test_health_server.py tests/test_realtime_phone_gateway.py -q
```

结果：

```text
82 passed, 1 warning
```

已执行：

```bash
uv run --with pytest pytest -q
```

结果：

```text
158 passed, 1 warning
```

warning 是既有 `websockets.legacy` 弃用提示，不影响本次部署判断。

## 本次必须同步的代码文件

这些文件属于当前功能变更，需要进入线上：

```text
app/call_control.py
app/main.py
app/postgres.py
app/realtime_phone_gateway.py
static/outbound-test.html
tests/test_call_control.py
tests/test_health_server.py
tests/test_postgres.py
tests/test_realtime_phone_gateway.py
docs/ai_call_python_db_operation_guide.md
```

核心变化：

- 页面增加 `callId` 输入。
- 外呼 context 传入 `callId / identityName / debtId`。
- Python 根据 `callId` 更新 Java 已初始化的 `public.call_record`。
- transcript 使用数据库简单格式：`{"turns":[{"role":"assistant","text":"..."},{"role":"user","text":"..."}]}`。
- 时间字段由数据库 `current_timestamp` 写入，跟随数据库会话时区。

## 不应直接同步覆盖的文件

```text
freeswitch-local/conf/vars.xml
```

本地当前值是：

```text
domain=192.168.0.107
external_rtp_ip=192.168.0.107
external_sip_ip=192.168.0.107
```

线上应保持或设置为：

```text
domain=111.229.146.182
external_rtp_ip=111.229.146.182
external_sip_ip=111.229.146.182
```

如果供应商白名单是 `111.229.146.182:19000/udp`，还必须确认：

```text
external_sip_port=19000
Docker 端口映射包含 19000/udp
服务器安全组和系统防火墙放通 19000/udp
```

## 线上预检结果

本机没有可用 SSH key：

```text
ssh root@111.229.146.182
Permission denied
```

公网端口探测：

```text
22/tcp 可连
9100/tcp 可连
```

HTTP 检查：

```text
GET http://111.229.146.182:9100/ready => 200
GET http://111.229.146.182:9100/health => 200
GET http://111.229.146.182:9100/outbound-test => 200
```

`/ready` 显示：

```text
server.host=0.0.0.0
server.port=9100
phone_codec=PCMA
outbound.enabled=true
outbound.endpoint_template=sofia/gateway/sip-provider/{destination}
outbound.event_socket_enabled=true
```

线上控制面 `9100/tcp` 当前公网可访问。真实测试阶段可临时使用，但不建议长期裸露。

## 真实电话第一通流程

1. Java 或数据库先初始化一条 `public.call_record`，得到 `callId`。
2. 打开线上页面：

```text
http://111.229.146.182:9100/outbound-test
```

3. 填写：

```text
destination={真实手机号}
caller_id_number=037123124845
caller_id_name=037123124845
endpoint=sofia/gateway/sip-provider/{真实手机号}
callId={Java 初始化出来的 call_record.id}
identityName=项目员工
debtId={对应 debt id}
originate_timeout_seconds=30
```

4. 发起前确认真实号码为人工测试号码，不要用批量名单。
5. 通话结束后检查：

```text
HTTP call status=completed
hangup_cause=NORMAL_CLEARING
sip_status=200
media_connected_at_ms 有值
public.call_record.status=4
public.call_record.started_at 有值
public.call_record.finished_at 有值
public.call_record.transcript.turns 有内容
```

## 部署前必须人工确认

- Termius 里 `111.229.146.182` 的 SSH 用户和认证方式。
- 线上代码目录。
- 线上启动方式，是 systemd、screen、nohup 还是脚本。
- 线上 FreeSWITCH 是否已经启用 `sip-provider.xml`。
- 线上 `external_sip_port` 是否是 `19000`。
- 线上 Docker 端口映射是否包含运营商白名单端口。
- 线上 `.env` 是否包含真实 `POSTGRES_DSN`、豆包 S2S 配置、ESL 密码。

## 建议部署顺序

1. 进入服务器后先备份线上当前目录。
2. 拉取或同步代码文件，但保留线上 `.env` 和公网 `vars.xml`。
3. 如果线上还没有真实 gateway，复制：

```bash
cp freeswitch-local/conf/sip_profiles/external/sip-provider.xml.template \
  freeswitch-local/conf/sip_profiles/external/sip-provider.xml
```

4. 重启 FreeSWITCH 和 Python 网关。
5. 检查：

```bash
docker exec sip_realtime_freeswitch fs_cli -x "sofia status gateway sip-provider"
docker exec sip_realtime_freeswitch fs_cli -x "sofia status profile external"
curl -sS http://127.0.0.1:9100/ready
```

6. 只打一通人工真实号码。
7. 把成功或失败样本补充回 `docs/sip-provider-profile.md`。

## 2026-05-24 部署记录

已将当前版本同步到 `111.229.146.182:/opt/recov_ten`，保留线上 `.env` 和 `freeswitch-local/conf/vars.xml`。

线上备份目录：

```text
/root/recov_ten_backup_20260524-161952-pre-current-version
```

已同步范围：

```text
app/
static/
tests/
docs/ai_call_python_db_operation_guide.md
docs/deploy-111229146182-current-version-checklist.md
```

线上验证：

```text
python -m compileall -q app tests 通过
systemctl restart recov-ten-gateway.service 成功
systemctl is-active recov-ten-gateway.service => active
GET http://127.0.0.1:9100/ready => 200
GET http://111.229.146.182:9100/health => 200
GET http://111.229.146.182:9100/outbound-test => 200
```

FreeSWITCH 真实线路状态：

```text
gateway sip-provider: NOREG / UP
Contact: sip:gw+sip-provider@111.229.146.182:5080;transport=udp
Ext-SIP-IP: 111.229.146.182
Ext-RTP-IP: 111.229.146.182
CODECS OUT: PCMA,PCMU
TEL-EVENT: 101
```

注意：

- 线上实际 `external_sip_port=5080`，不是文档中待确认的 `19000`。
- 本次未重启 FreeSWITCH，只重启了 Python 网关。
- 服务器 `.venv` 未安装 `pytest`，线上没有执行 pytest；本地全量测试结果为 `158 passed, 1 warning`。
- 第一次窄范围同步漏了 `app/business_dialog_style.py`，导致启动时缺少 `numbered_business_fact_boundary_rules`；已改为同步完整 `app/` 后修复。

## 2026-05-24 页面默认线路修正

真实线路测试时发现 `/outbound-test` 默认选中 `sip-provider 沙箱：正常接通`，页面加载和切换场景会自动把 endpoint 覆盖为：

```text
sofia/gateway/sip-provider-sandbox/15800967789
```

这会导致用户即使填写了真实手机号，也仍然走沙箱线路。

已调整 `static/outbound-test.html`：

- 默认场景改为 `sip-provider 真实线路：手动号码`。
- 默认 caller 改为 `037123124845`。
- 填写 destination 时，endpoint 自动生成：

```text
sofia/gateway/sip-provider/{destination}
```

线上已同步该静态页，备份目录：

```text
/root/recov_ten_static_backup_20260524-164412
```

验证：

```text
GET http://111.229.146.182:9100/outbound-test
页面包含 manual-sip-provider selected
页面不再包含 sandbox-answer selected
GET http://111.229.146.182:9100/ready => 200
```
