# ADR-011：FILE_SHARE 官方上传与双副本

## 状态

已采纳，取代 ADR-007；对应 RF-PATCH-002 退役。

## 背景

RAGFlow v0.26.4 已提供 Dataset Document multipart upload、parse/reparse、状态与 Chunk API。继续维护一次性 source ticket、`external://` 虚拟文档和两个 task executor 分支会重复官方能力，并扩大 Gateway 与上游补丁面。

## 决策

- 企业文件服务器仍是 EAM 原始文件的权威源，Gateway 继续保存租户、版本、SHA-256、ACL、挂载根 ID 和相对路径。
- Gateway 在校验文件路径、大小和 SHA-256 后，通过 `POST /api/v1/datasets/{dataset_id}/documents` multipart 上传同一文件句柄。
- RAGFlow 保存自己的解析/检索副本，并通过 `POST /api/v1/datasets/{dataset_id}/chunks` 执行首次解析或重解析。
- Citation 原文访问仍由 Gateway 按 EAM 用户 ACL 读取权威文件；Gateway 不直接访问 RAGFlow 的内部存储实现。
- 删除 source-ticket 运行链、`external://`、自定义 external document API 和 task executor 分支，不提供双轨、feature flag 或运行时回退。
- FILE_SHARE v3.1.0、Query v2.9.0、Callback v1.0.0 的 EAM 外部字段、URL、状态和事件保持不变。

## 数据与回滚

- 现有数据均视为测试数据，不迁移、不清空，也不主动删除 RAGFlow 已有文档。
- 已存在的 `ext_source_ticket` 表不执行 `DROP`；新代码不再创建或使用它。
- 回滚方式是撤销本工作包的隔离代码提交；不通过恢复 source-ticket 双轨回滚，也不删除已上传副本。

## 安全边界

- FILE_SHARE 路径解析必须限制在配置根目录内并拒绝符号链接逃逸。
- 上传前必须以分块方式校验登记 SHA-256；校验失败不得创建可检索文档。
- RAGFlow `DONE` 不是 Gateway `qualityStatus=passed`，Quality Gate 仍决定是否可检索和是否发送成功终态回调。
