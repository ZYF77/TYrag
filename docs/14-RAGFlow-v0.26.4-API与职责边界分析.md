# RAGFlow v0.26.4 API 与职责边界分析

## 判定方法

本文件是实施前的事实边界，不是对官方 DEV 的预测。实现者依序查看本地 v0.26.4 源码和本机 HTTP 行为、固定 tag 源码、tag 文档，再参考官网 DEV。当前 tag 文档把 document update 写作 PUT，而本地 `backward_compat.py` 与 tag 源码已将旧 PUT 指向 PATCH；因此主路固定 PATCH，且只记录该漂移。

## 真实调用链

### 持久 FILE_SHARE 文档

`EAM FILE_SHARE v3.1.0 POST /enterprise/api/v3/documents` → Gateway HMAC binding、事件/版本幂等、FILE_SHARE 只读原件检查、业务 metadata → `POST /api/v1/datasets/{dataset_id}/documents` multipart → RAGFlow document → `POST /api/v1/datasets/{dataset_id}/chunks`（body：`document_ids`）解析/重解析 → `GET ...documents?id=` 和 `GET ...documents/{document_id}/chunks` 读取事实 → Gateway quality gate → Gateway 保存外部状态并按 Callback v1.0.0 投递终态。

`RF-PATCH-002` 的旧链（Gateway 注册 `external://` → RAGFlow 私有 `/documents/external`/换票 → executor 临时下载）不在目标调用链；首包直接移除，不给 fallback。

### 问询与历史

`EAM Query v2.9.0 POST /conversations` → Gateway JWT/tenant/user → `POST /api/v1/chats/{chat_id}/sessions` → Gateway 保存 business conversation 与 RAGFlow session 对应。`POST /conversations/{id}/messages` → Gateway 验证 conversation ownership、`clientMessageId`、ACL 与 quality 交集 →（必要时）`POST /api/v1/retrieval` → `POST /api/v1/chat/completions` → Gateway 规范化 JSON/SSE、持久 run/message 状态、Citation Snapshot → EAM。业务历史以 Gateway 已持久化的业务状态为事实源，不按 citations 重新判定。

### 单会话附件

`EAM 同一 v2 message multipart` → Gateway JWT、会话所有权、MIME/大小/TTL → `POST /api/v1/documents/upload` → 同一轮 `POST /api/v1/chat/completions(files[])` → `RF-PATCH-006` 认证单对象 DELETE/TTL orphan 重试 → Gateway 保留 citation snapshot。附件不是 FILE_SHARE 文档，也不得进持久 dataset、触发 FILE_SHARE callback 或形成跨会话 retrieval。DELETE 是 v0.26.4 官方缺口的最小补丁，不是通用文件 API。

## 责任矩阵

| 事实/动作 | EAM | Gateway | RAGFlow |
|---|---|---|---|
| 外部协议、用户身份 | 调用既有 v2/v3 | 验证 HMAC/JWT、tenant/user 隔离 | 不暴露给 EAM |
| 原件入 KB | 提交 FILE_SHARE 事实 | 原件只读检查、幂等、multipart 编排 | 官方持久 upload/document |
| 解析、chunk、重解析 | 轮询既有状态 | 质量门、状态映射、callback | 官方 chunks/parse 与状态 |
| 权限检索 | 提供用户身份 | 先算 ACL/业务范围，再请求 | 在给定过滤条件内 retrieval |
| 会话/SSE | 消费 v2 wire | run、幂等、业务历史、SSE contract | session/completion 引擎 |
| Citation/原件访问 | 展示已授权证据 | snapshot、再授权、Range | chunk/reference 原始结果 |

## 禁止的职责漂移

- Gateway 不重建 parser、vector store、chunk engine、对话推理引擎或官方 session store。
- RAGFlow 不持有 EAM 的 JWT/HMAC、业务幂等键、业务状态、callback 策略或外部 citation URL。
- ACL 必须在 retrieval/completion 前形成条件交集；不得全库召回后过滤。
- `citations` 与消息业务状态独立：附件观察可以 `completed` 且无 citation；无可靠证据不能靠 citation 空数组推导。

## 兼容性结论

EAM Query `v2.9.0`、FILE_SHARE `v3.1.0` 和 Callback `v1.0.0` 路径、字段、HTTP 语义和 SSE 事件不变。legacy v1/document/demo API 不在本轮移除。若发现官方 API 不能保留该外部契约，只能输出“协议变更说明”（差异、迁移、回滚、E2E 证据）并等待用户确认；不能在实现中悄然改线。
