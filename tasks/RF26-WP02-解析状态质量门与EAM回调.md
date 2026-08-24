# RF26-WP02 解析状态质量门与 EAM 回调

## 目标

以 RAGFlow v0.26.4 的 document/chunk 真实事实驱动 FILE_SHARE 状态，Gateway 在 retrievable 前执行质量门，并按 Callback v1.0.0 投递终态；不伪造官方无法提供的独立索引时刻。

## 范围与非范围

范围：官方 parse/reparse 发起、documents/chunks 轮询、质量判定、外部状态映射、outbox/callback 和失败可观察性。非范围：自建解析器、改 RAGFlow task 状态枚举、callback URL 入参化、改变 Callback v1.0.0 payload、因 callback 失败回滚 ingestion、30 服务器验收。

## 真实调用链

WP01 的官方 document ID → Gateway `POST /api/v1/datasets/{dataset_id}/chunks`（body：`document_ids`）→ RAGFlow 解析/切片 → Gateway `GET /api/v1/datasets/{dataset_id}/documents?id={document_id}` 与 `GET .../documents/{document_id}/chunks` → Gateway 依据官方状态和 chunk 事实运行质量门 → 持久 `parseCompleted/indexCompleted/retrievable` 的诚实字段 → outbox → EAM Callback v1.0.0。`retrievable=true` 只能由 Gateway 的“官方成功事实 + quality passed”得出。

## 接口与责任归属

RAGFlow：parse、chunk、官方 document 状态。Gateway Parsing/Quality：状态解释、质量门、外部状态、outbox。Gateway Callback：server-side sourceSystem endpoint、独立签名、重试 1/5/30/120/600 秒最多 8 次、dead-letter。EAM：消费既有 terminal callback。callback URL 永不来自登记请求体。

## 精确实施任务

1. 将 parse/reparse 统一调用官方 chunks endpoint，保留 document ID 映射；不调用旧 external parse 路径。
2. 明确轮询终止条件：官方失败→`failed`；官方未完成→处理中；官方完成但 chunks/质量不合格→`no_reliable_evidence` 或既有质量失败状态；只有合格才 `retrievable`。
3. 质量门只使用已约定的最小 chunk/解析事实，记录失败原因代码，日志不含全文/Prompt。
4. 保持 v3 status 和 sync-status 的字段/HTTP 语义，新增内部证据不得泄露 RAGFlow ID。
5. terminal 状态写 outbox 后回调；失败仅影响 delivery，绝不倒退已成功文档状态。
6. 测试 parse 成功、空/劣质 chunks、RAGFlow 失败、callback 5xx 重试/dead-letter 及 callback URL 注入拒绝。

## 依赖、验收与回滚

依赖：WP01 有官方 uploaded document。验收：本机 E2E 可观察 upload→parse→quality→retrievable/callback；质量失败不能被 retrieval 使用；callback payload 仍 v1.0.0 且签名不泄密。回滚：恢复质量门/状态映射与 callback worker 到上一已测版本；不删除 outbox、文档或历史 callback 记录。

## Agent 目录所有权

Parsing Agent：RAGFlow ingestion 配置、解析 profile、质量服务。File Sync Agent：sync 状态事实。Platform/Lead：outbox 部署与共享配置。任何人不得改官方状态枚举或数据库模型。
