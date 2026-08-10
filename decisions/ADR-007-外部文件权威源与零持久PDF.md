# ADR-007：外部文件权威源与零持久 PDF

## 状态

已采纳，P0 已实现；实际部署仍需填写只读挂载和服务间密钥。

## 决策

- v2 S3/MinIO 入站契约保持不变。
- v3 仅接受 `FILE_SHARE`：企业文件服务器是原始 PDF 的唯一权威源；企业 Gateway SQLite 只保存租户、版本、哈希、ACL、挂载根 ID 和相对路径。
- Gateway 为一次解析签发一次性、短 TTL 的不透明票据。RAGFlow 通过内部接口消费票据，把内容写入临时目录、校验 `X-Source-SHA256`，解析结束后由临时目录清理；RAGFlow 和 MinIO 不保存原始 PDF。
- 票据消费不允许复用；解析重试必须由 Gateway 签发新票据。
- 原始文件访问通过 Gateway 的已授权 citation source 端点，支持单 Range 与 `If-Range`，只服务精确的 `sourceVersionId`。

## 影响与回滚

本 ADR 修改的是 Gateway 外围适配和 RAGFlow 解析入口，不修改 RAGFlow 官方数据库迁移、官方对象存储抽象或 v2 OpenAPI。回滚时停止 v3/票据路由并保留 v2；上游补丁可单独撤销。

## 安全边界

文件服务器以只读挂载方式接入，应用不持有 SMB 凭据；路径必须位于部署配置的 root 下。内部票据接口可由部署配置 `TYRAG_EXTERNAL_SOURCE_INTERNAL_KEY` 进一步绑定服务间密钥。
