# M1-E Harness 运行说明

M1-E 分成两个边界：浏览器 Harness 只持有用户 JWT，用于用户会话、Gateway 真 SSE 和权限诊断；服务侧 producer 才负责文档事件与文档状态的 HMAC 请求。浏览器不会调用 HMAC-only 文档接口，也不会接收、保存或发送 HMAC secret。

## 启动顺序

1. 先启动 Gateway 及其依赖，确认用户 JWT 能访问 `/enterprise/api/v2/conversations`。
2. 文档同步时，在受信任的服务端或运维 CLI 环境设置 producer 变量，并从仓库根目录运行 producer 的 `submit`；需要查询时运行 `status`。producer 使用现有 `enterprise.gateway.auth.service_auth.sign_request`，不会把 secret 放入请求或输出。
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
$env:M1E_PRODUCER_BASE_URL = 'http://127.0.0.1:5188/enterprise/api/v2'
$env:M1E_PRODUCER_KEY_ID = '<service-key-id>'
$env:M1E_PRODUCER_SECRET = '<service-secret-from-secret-store>'
$env:M1E_PRODUCER_TENANT_ID = '<tenant-id>'
$env:M1E_PRODUCER_SOURCE_SYSTEM = '<source-system>'
python -m enterprise.scripts.m1e_document_producer submit --payload-file .\document-event.json
python -m enterprise.scripts.m1e_document_producer status --external-document-id '<external-document-id>' --source-version-id '<source-version-id>'
```

变量类型：`BASE_URL` 为 HTTP(S) 地址，`KEY_ID`、`TENANT_ID`、`SOURCE_SYSTEM` 为非 secret 标识，`SECRET` 为仅服务端可读的 HMAC secret，`TIMEOUT_SECONDS`（可选）为正数。不要把 secret 写入 payload、浏览器 `VITE_*` 变量、命令行参数、日志或提交内容；使用 secret manager/进程环境注入，并在 producer 进程退出后清理环境。

## Mock 边界

```powershell
cd enterprise/web
$env:VITE_API_MODE = 'mock'
$env:VITE_UI_MODE = 'harness'
pnpm dev
```

mock 模式保留文档表单、轮询和 MSW 数据，仅作为 UI contract test，不能作为 Integration 证据。生产或真实联调应使用 `gateway` 浏览器 Harness 加服务侧 producer；浏览器端只发送用户 JWT，文档请求必须携带 `X-TY-Timestamp`、`X-TY-Key-Id` 和 `X-TY-Signature`。
