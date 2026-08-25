# RF26-WP01 FILE_SHARE 官方上传与 RF-PATCH-002 退役

## 目标

让 FILE_SHARE v3.1.0 新文档在首个请求直接走 RAGFlow 官方 multipart upload，完整删除 `RF-PATCH-002` 所需的虚拟外部文档/换票/解析分支；保留 EAM 的既有请求、响应、状态 URL 语义与历史数据。

## 范围与非范围

范围：Gateway sync 的 FILE_SHARE 原件读取、元数据映射、幂等账本、官方上传客户端；RF-PATCH-002 四个上游文件及其注册路由的独立撤销；对应 unit/contract/E2E。非范围：DROP 历史表、迁移/删除旧 `external://` 记录或对象、FILE_SHARE 以外新入口、双轨/feature flag/fallback、修改 v1/document/demo API、改变 FILE_SHARE v3.1.0 wire contract。

## 真实调用链

`EAM → POST /enterprise/api/v3/documents → Gateway`：先 HMAC credential binding、tenant/source binding、业务 key `(tenantId,sourceSystem,externalDocumentId,sourceVersionId)`、版本哈希与只读 root 安全检查；随后 Gateway 以受控 multipart 流调用 `POST /api/v1/datasets/{dataset_id}/documents`，写入业务 metadata（含 ACL/设备身份），持久关联外部 ID 与 RAGFlow document ID。成功后转 WP02 的 official chunks/状态链。

不再允许：`/documents/external`、`/external-source`、`external://`、一次性 ticket 或 executor 中 `fetch_external_source`。原始文件在官方 upload 的 RAGFlow 管理对象中存放；FILE_SHARE 仍是 EAM 的权威业务原件，citation 对外访问仍须 Gateway 授权。

## 接口与责任归属

EAM：保持 FILE_SHARE v3.1.0 payload/HMAC。Gateway：验签、路径/版本安全、幂等、multipart 编排、外部 ID 映射；不得暴露 RAGFlow ID。RAGFlow：官方 document upload 和持久文档。Lead：确认上游 patch 删除清单、`manifest`/ADR 的退役登记；File Sync Agent：仅改 `enterprise/gateway/sync` 和其测试。

## 精确实施任务

1. 将正式 v3 upsert 客户端改为官方 multipart documents endpoint；明确 content-disposition、文件名、dataset ID、metadata 和超时，不读/打印完整原件。
2. 让同 eventId/同 hash 重放返回既有 202，不重复 upload；冲突保持既有 409，不能通过上传差异绕过。
3. 删除 Gateway 指向 `/documents/external`、`external-source` 与 `external://` 的生产调用和测试 fixture；不得保留条件分支。
4. 撤销 RF-PATCH-002 四个受登记上游文件的代码，并由 Lead 更新 patch registry/ADR 的退役状态；不动 RF-PATCH-003/004。
5. 不处理旧历史数据，也不为旧 virtual record 新增兼容层；只保证本包不主动迁移、删除或重新上传历史记录。
6. 为官方 upload 成功、重复幂等、哈希冲突、root 路径逃逸/缺文件建立测试。

## 依赖、验收与回滚

依赖：WP00 的 API/补丁清单。验收：新 FILE_SHARE E2E 仅出现官方 multipart 路径；源码/请求日志无旧 external 路径；HMAC 与外部字段不变；历史表/对象计数未被删除；`RF-PATCH-002` 不再应用。回滚：恢复本包独立变更前的 Gateway 调用和已登记的 RF-PATCH-002 patch；不操作历史数据。回滚不是启用双轨。

## Agent 目录所有权

File Sync Agent：`enterprise/gateway/sync`、对象读取适配及测试。Lead：`ragflow/**`、`patches/manifest.yaml`、ADR/公共契约。禁止非 Lead 改官方迁移、模型或主 OpenAPI。
