# CHANGE-REQUEST：M3-C transient attachment 垂直切片

## 状态

Open。当前实现是 Enterprise Gateway 内部/实验性能力，不改变公共 v2 OpenAPI 基线，不能据此声明 P1 或 Integration 已验收。

## 原因

公共 `integration-openapi-v2.yaml` 仍将 conversation attachment 标记为 `planned`。本工作包需要先验证对象存储边界、一次性下载、租户/会话归属和临时对象清理，但 M2 当前 blocked，不能用 mock、skip 或本地 fixture 代替真实 Integration 证据。

## 最小实现

1. Gateway 接收严格的 base64 attachment 请求，校验允许的 MIME、扩展名、大小和安全文件名。
2. Gateway 使用现有 `S3SourceAdapter` 的凭据边界写入配置的 S3/MinIO 临时前缀；凭据和对象存储 URL 不进入响应。
3. Gateway 在 Enterprise SQLite 中仅保存租户、会话、用户、对象坐标、哈希、大小、TTL、次数和审计元数据，并返回短时效一次性下载票据。
4. 下载票据按租户/用户绑定校验；下载经 Gateway 读取并校验哈希、大小和 MIME，票据成功消费一次后不可重放。
5. 清理 worker 删除过期对象；对象存储失败按有限重试记录 `next_retry_at`、错误码和审计元数据。

## 部署配置

- `S3_TRANSIENT_BUCKET`（未设置时使用现有 `S3_BUCKET`）和可选的 `S3_TRANSIENT_PREFIX`。
- `ENTERPRISE_ATTACHMENT_MAX_SIZE_BYTES`、`ENTERPRISE_ATTACHMENT_TTL_SECONDS`、`ENTERPRISE_ATTACHMENT_MAX_DOWNLOADS`。
- `ENTERPRISE_ATTACHMENT_RETRY_ATTEMPTS`、`ENTERPRISE_ATTACHMENT_RETRY_DELAY_SECONDS`、`ENTERPRISE_ATTACHMENT_CLEANUP_INTERVAL_SECONDS`。

这些配置不包含凭据；S3 endpoint 和凭据继续由现有对象存储适配器从部署环境读取。

## 契约与兼容性

- 未修改 `contracts/integration-openapi-v2.yaml`、官方 OpenAPI 基线、`ragflow/**`、官方迁移或根锁文件。
- 路由使用 `include_in_schema=False`，仅作为内部/实验性垂直切片，不承诺公共 wire contract。
- `indexPolicy` 服务端固定为 `never`，不会进入文档 ingestion、持久 embedding 或 RAGFlow 文档映射。

## 待晋升条件

- M2 解 block 后补充真实 MinIO/S3、Gateway、租户 ACL 和清理验证。
- 确认公共请求/响应字段、错误码、TTL 和最大下载次数后，再单独更新 v2 OpenAPI、错误码和契约测试。
- 将一次性票据或签名 URL 的浏览器使用方式纳入正式客户端契约；当前客户端只能使用 Gateway URL。

## 回滚

移除 transient attachment router 和 cleanup worker，并删除 Enterprise attachment 代码即可；既有 v1/v2 文档同步、FILE_SHARE 票据和公共 OpenAPI 不受影响。
