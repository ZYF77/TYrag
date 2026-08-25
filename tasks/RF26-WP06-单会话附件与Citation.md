# RF26-WP06 单会话附件与 Citation

## 目标

维持 EAM Query v2.9.0 的同一消息接口，让附件只服务当前会话/当前轮的 RAGFlow 官方 files/completion 能力；citation 保留为 Gateway 的持久 snapshot 与授权访问，不把附件变成 FILE_SHARE 持久入库或检索证据。

## 范围与非范围

范围：v2 message multipart、附件 ownership/MIME/大小/TTL、`POST /api/v1/documents/upload`、`files[]` completion、清理、Citation Snapshot 与授权 source/Range。非范围：新增 EAM URL/字段、附件持久 embedding、附件跨会话复用、FILE_SHARE v3 Office 入库、OCR/Office parser 重造、根据 citations 改消息状态、修改 RF-PATCH-003 的删除决定。

## 真实调用链

`EAM v2 POST /conversations/{id}/messages multipart` → Gateway JWT/tenant/user、conversation ownership、限制/TTL、一次上传幂等 → `POST /api/v1/documents/upload` → 同一 run 的 `POST /api/v1/chat/completions(files[])` → RAGFlow 返回 knowledge references → Gateway 将经 ACL/版本复核的 chunk 映射为 immutable Citation Snapshot → JSON/SSE 与历史。附件清理在 completion terminal 后执行；附件本身不生成 citation，只有知识库 chunk 才能生成 citation。

## 接口与责任归属

EAM：沿用 Query v2.9.0 multipart。Gateway：会话所有权、TTL、上传账本、单轮文件映射、citation snapshot、再授权下载/Range。RAGFlow：官方 upload 与本轮 files/completion。FILE_SHARE 仍只通过 WP01 的持久文档入口；两条链不得混用 callback、状态或数据集。

## 精确实施任务

1. 确认 multipart 与 JSON 纯文本保持同一 v2 route 和 `clientMessageId` 幂等；重放不得重复上传。
2. 调用官方 documents/upload，并把返回 descriptor 只传给当前 completion 的 `files[]`；stream 和 JSON 路径相同。
3. 在读取 multipart 文件体前检查 conversation owner/archived，并在现有 lock 内复核；对 MIME、数量、大小、TTL、失败 cleanup 做最小安全检查，不记录文件字节/全文到审计。
4. 保持 RF-PATCH-003 现状，只对本包 files 调用链回归；本包不得借此删除/扩大 patch。
5. 建立 citation snapshot：外部 citation ID、文档版本、页码/bbox/excerpt；每次读取 source/citation 时重新 ACL/版本校验，支持既有 Range，不泄露内部 object path。
6. v0.26.4 缺少 upload 单对象 DELETE，因此由 `RF-PATCH-006` 提供认证、tenant-scoped、32-hex ID 的幂等删除；Gateway 禁止访问 `STORAGE_IMPL`，删除失败保留 orphan ledger，后台只在 TTL 后重试。
7. 测试单会话隔离、上传一次/幂等、过期/越权附件、附件 reference 不形成 citation、附件问答 `completed` 且 citations 空、知识库 citation 可授权访问。

## 依赖、验收与回滚

依赖：WP04 run/SSE，WP05 Grounding/status 口径，WP03 citation ACL。验收：附件从不进入 dataset/retrieval 持久索引；历史 citation snapshot 稳定且越权下载拒绝；外部 v2.9.0 不变。回滚：恢复附件 adapter/cleanup 到上一已测实现，保留已有 snapshot 与业务消息；不批量删除上传对象或历史。

## Agent 目录所有权

Retrieval Agent：`enterprise/gateway/query`。File Sync Agent：只协助 source access，不能把附件纳入 FILE_SHARE。Lead：RF-PATCH-003 及公共契约。禁止改官方文件存储/数据库模型。
