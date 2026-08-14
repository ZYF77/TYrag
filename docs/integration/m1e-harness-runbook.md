# M1-E Harness 运行说明

M1-E 分成两个边界：浏览器 Harness 只持有用户 JWT，用于用户会话、Gateway 真 SSE 和权限诊断；服务侧 producer 才负责 FILE_SHARE v3 文档登记与状态查询的 HMAC 请求。浏览器不会调用 HMAC-only 文档接口，也不会接收、保存或发送 HMAC secret。

## 启动顺序

1. 先启动 Gateway 及其依赖，确认用户 JWT 能访问 `/enterprise/api/v2/conversations`。
2. 文档同步时，在受信任的服务端或运维 CLI 环境设置 producer 变量，并从仓库根目录运行 producer 的 `submit`；`202` 为瘦身受理回执（无 `statusUrl`）。需要诊断查询时可用 `status --external-document-id`，或自行拼出诊断 status URL 后用 `status --status-url`。正式 EAM 对接依赖 Gateway 终态回调。producer 使用现有 `enterprise.gateway.auth.service_auth.sign_request`，不会把 secret 放入请求或输出。
3. 再启动浏览器 Harness：

   ```powershell
   cd enterprise/web
   $env:VITE_API_MODE = 'gateway'
   $env:VITE_UI_MODE = 'harness'
   pnpm dev
   ```

   在页面中只注入用户 JWT（运行期 Bearer）。Gateway 模式的文档区域会明确显示“服务侧 producer”，不会伪造文档同步成功。

## Producer

使用不含 secret 的 payload 文件：

```powershell
$env:M1E_PRODUCER_BASE_URL = 'http://127.0.0.1:5188/enterprise/api/v3'
$env:M1E_PRODUCER_KEY_ID = '<service-key-id>'
$env:M1E_PRODUCER_SECRET = '<service-secret-from-secret-store>'
$env:M1E_PRODUCER_TENANT_ID = '<tenant-id>'
$env:M1E_PRODUCER_SOURCE_SYSTEM = '<source-system>'
python -m enterprise.scripts.m1e_document_producer submit --payload-file .\document-event.json
python -m enterprise.scripts.m1e_document_producer status --external-document-id '<externalDocumentId>' --source-version-id '<sourceVersionId>'
```

`document-event.json` 必须符合 `contracts/file-share-v3.yaml` 的 FILE_SHARE v3 请求体，`source.kind` 必须为 `FILE_SHARE`，PDF 由双方约定的只读文件共享提供，producer 只提交坐标、SHA-256 和元数据。`submit` 输出为 3.1.0 受理回执字段（`operationId`、`externalDocumentId`、`sourceVersionId`、`deduplicated`、`updatedAt`）；诊断状态请用 `--external-document-id` 查询。

变量类型：`BASE_URL` 为 HTTP(S) 地址，`KEY_ID`、`TENANT_ID`、`SOURCE_SYSTEM` 为非 secret 标识，`SECRET` 为仅服务端可读的 HMAC secret，`TIMEOUT_SECONDS`（可选）为正数。不要把 secret 写入 payload、浏览器 `VITE_*` 变量、命令行参数、日志或提交内容；使用 secret manager/进程环境注入，并在 producer 进程退出后清理环境。`SECRET` 必须由 Gateway 部署方通过安全渠道交付，不能从 Gateway API 获取。

## Mock 边界

```powershell
cd enterprise/web
$env:VITE_API_MODE = 'mock'
$env:VITE_UI_MODE = 'harness'
pnpm dev
```

mock 模式保留文档表单、轮询和 MSW 数据，仅作为 UI contract test，不能作为 Integration 证据。生产或真实联调应使用 `gateway` 浏览器 Harness 加服务侧 producer；浏览器端只发送用户 JWT，文档请求必须携带 `X-TY-Timestamp`、`X-TY-Key-Id` 和 `X-TY-Signature`。
