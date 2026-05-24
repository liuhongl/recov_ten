# 111.229.146.182 当前版本部署前检查

本文档用于把当前本地版本部署到 `111.229.146.182` 做真实电话单通验证。目标是受控测试，不是生产上线或批量外呼。

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
