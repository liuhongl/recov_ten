# recov_ten 通话录音上传部署与验收

## 范围

本说明只覆盖完整通话混音 WAV 的长期留存链路：

FreeSWITCH `record_session` 生成 WAV -> recov_ten 读取宿主机文件 -> 上传 MinIO -> 写入 `public.sys_oss` -> 回填 `public.call_record.recording_oss_id`。

转人工双路 WAV 仍然是人工转写临时材料，继续由 `features.recording_enabled` 和 `features.recording_dir` 控制，不进入 `sys_oss`。人工转写成功后会删除这两份临时 WAV；转写失败时会保留，便于排查或重试。

## 数据库变更

上线前执行：

```sql
alter table public.call_record
add column if not exists recording_oss_id bigint;

comment on column public.call_record.recording_oss_id
is '完整通话混音录音对应的 sys_oss.oss_id';
```

同仓库可执行文件：`docs/sql/2026-06-03-call-recording-upload.sql`。

## 必要配置

完整录音上传依赖 PostgreSQL。开启上传时必须同时开启：

```env
POSTGRES_ENABLED=true
CALL_RECORDING_ENABLED=true
CALL_RECORDING_UPLOAD_ENABLED=true
CALL_RECORDING_DIR=/var/lib/freeswitch/recordings
CALL_RECORDING_HOST_DIR=/opt/recov_ten/recordings
CALL_RECORDING_OBJECT_PREFIX=recordings
CALL_RECORDING_UPLOAD_TIMEOUT_SECONDS=30
```

路径含义：

- `CALL_RECORDING_DIR` 是 FreeSWITCH 容器内路径。
- `CALL_RECORDING_HOST_DIR` 是 recov_ten 进程可读取的宿主机路径。
- `CALL_RECORDING_UPLOAD_TIMEOUT_SECONDS` 是单次 MinIO 上传请求超时，默认 30 秒。
- 线上 Docker 挂载应把宿主机 `/opt/recov_ten/recordings` 映射到容器 `/var/lib/freeswitch/recordings`。

`public.sys_oss_config` 必须有一条 `status = '0'` 的活跃配置。recov_ten 会读取：

- `endpoint`
- `bucket_name`
- `access_key`
- `secret_key`
- `prefix`
- `is_https`
- `region`
- `domain`

`access_key` 和 `secret_key` 不应出现在日志中。

其中 `endpoint`、`bucket_name`、`access_key`、`secret_key` 不能为空。`is_https` 支持 `Y`、`true`、`1`、`yes` 这类真值。`domain` 可为空；非空时可填裸域名，也可填带 `http://` / `https://` 的完整访问前缀。任一必填字段为空时，录音上传会跳过回填并输出 `recording_upload_failed` 日志。

## 上线顺序

1. 确认 `public.sys_oss_config` 有活跃 MinIO 配置。
2. 确认 `public.call_record` 尚未或已经具备 `recording_oss_id` 字段。
3. 执行 `docs/sql/2026-06-03-call-recording-upload.sql`。
4. 创建宿主机目录 `/opt/recov_ten/recordings`，确认 recov_ten 运行用户可读。
5. 给 FreeSWITCH 容器增加挂载：`/opt/recov_ten/recordings:/var/lib/freeswitch/recordings`。
6. 先保持 `CALL_RECORDING_UPLOAD_ENABLED=false` 启动，确认 `/ready` 配置符合预期。
7. 开启 `CALL_RECORDING_ENABLED=true`，打测试电话确认宿主机能看到 WAV。
8. 开启 `CALL_RECORDING_UPLOAD_ENABLED=true`，再打一通真实测试电话。

## 验收

测试电话结束后检查：

```sql
select id, recording_oss_id
from public.call_record
where id = :call_id;
```

```sql
select oss_id, file_name, original_name, file_suffix, url, service, ext1
from public.sys_oss
where oss_id = :recording_oss_id;
```

期望：

- `call_record.recording_oss_id` 有值。
- `sys_oss.service = 'minio'`。
- `sys_oss.file_name` 形如 `recordings/{tenantId}/{yyyyMMdd}/{callId}.wav`，如果 `sys_oss_config.prefix` 非空，会带该前缀。
- MinIO 中存在对应 object。
- 业务侧可以通过 `ossId` 获取或播放录音。

## 失败边界

录音上传发生在 transcript 写入成功之后。上传失败只会导致 `recording_oss_id` 为空并输出 `recording_upload_failed` 日志，不会回滚 `call_record.status` 或 `call_record.transcript`。

上传前会短暂等待本地 WAV 文件变为非空且大小稳定，避免 FreeSWITCH 刚挂断时文件尚未完全落盘导致误读。超过等待窗口仍不可读时，本次上传按失败处理。

如果 `call_record.recording_oss_id` 已经有值，recov_ten 会在上传前跳过处理，不会重复上传，也不会覆盖已有业务录音关联。

如果需要关闭录音：

```env
CALL_RECORDING_ENABLED=false
CALL_RECORDING_UPLOAD_ENABLED=false
```

如果只想停止上传但保留本地完整 WAV：

```env
CALL_RECORDING_UPLOAD_ENABLED=false
```
