# RF26-WP00 基线与官方 API 契约

## 目标

冻结后续收敛唯一可用的 RAGFlow `v0.26.4` 本地源码/API 事实，给 WP01–WP07 提供可复核的调用路径与协议边界；不在本包改任何运行代码、契约或上游文件。

## 范围与非范围

范围：核对运行版本、源码 tag、`document_api`、`backward_compat.py`、dataset/chunk/retrieval/chat API，以及现有 Gateway 客户端实际发出的路径；把 PUT/PATCH 漂移、RF-PATCH-002/003/004 的状态写进基线记录。非范围：升级 tag、变更 EAM v2.9.0/FILE_SHARE v3.1.0/Callback v1.0.0、调用 30 服务器、改主 OpenAPI 或 `patches/manifest.yaml`。

## 真实调用链

以 `Gateway client → RAGFlow HTTP API` 为审计单位：document multipart `POST /api/v1/datasets/{dataset_id}/documents`；parse/reparse `POST /api/v1/datasets/{dataset_id}/chunks`（body：`document_ids`）；文档事实 `GET .../documents?id={document_id}`；chunk 事实 `GET .../documents/{document_id}/chunks`；检索 `POST /api/v1/retrieval`；session `POST /api/v1/chats/{chat_id}/sessions`；completion `POST /api/v1/chat/completions`；附件 `POST /api/v1/documents/upload`。RAGFlow 负责这些资源语义，Gateway 负责外部协议与安全/业务映射。

## 接口与责任归属

RAGFlow 拥有上述公开资源、解析、检索与 completion 的运行语义；Gateway 只拥有 EAM 协议适配、认证/授权、幂等、质量门、回调、业务历史与 Citation Snapshot。EAM 继续只消费 Query v2.9.0、FILE_SHARE v3.1.0 和 Callback v1.0.0，不能直连 RAGFlow。

Document update 主路是本地/tag 源码的 `PATCH /api/v1/datasets/{dataset_id}/documents/{document_id}`。tag 文档列出 PUT 是已知文档漂移，不能新建 PUT alias 或补丁。

## 精确实施任务

1. 记录 `VERSION`、tag commit、运行镜像/entrypoint 的一致性证据，且不回显 secret。
2. 逐个比对上述八条 API 的源码路由、请求字段和 Gateway 客户端路径；把“源码/本机 E2E/tag 文档/DEV”来源标注为证据等级。
3. 盘点 `RF-PATCH-002` 四个上游文件和 Gateway 的 `/documents/external`、`external-source`、`external://` 调用点，形成 WP01 的删除清单；只盘点，不删除。
4. 确认 RF-PATCH-003/004 不属于本包；记录保留原因与独立回归测试。
5. 形成静态契约测试清单，确保后续无旧 external 路径、无 PUT alias、无未授权 EAM 协议改动。

## 依赖、验收与回滚

依赖：无。验收：基线文档明确所有八条路径、PATCH 漂移、补丁边界；静态检查能定位旧路径；`git diff --check` 通过。回滚：本包只有证据/计划记录，撤回记录即可，不能触发代码或数据回滚。

## Agent 目录所有权

Lead：上游版本、`patches/manifest.yaml`、公共基线与主 OpenAPI。各实现 Agent 只能消费本包事实；不得改官方迁移、模型、锁文件或公共 Compose。
