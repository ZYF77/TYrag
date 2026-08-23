# External Integration Contract v2.1 候选基线

## 1. Preflight

- 成功标准：冻结并实现设备管理系统与 TYrag 的 P0 边界，且以自动化 gate 证明。
- 仓库/基线：`TYrag`，`8e877950636cecd8e4150344400d494389fecde7`。
- 允许修改：`contracts/`、`decisions/`、`docs/`、`tasks/`、`README.md`、`enterprise/`。
- 明确不改：`ragflow/` 上游、官方迁移/模型、根依赖锁文件、客户业务 PostgreSQL 和 RAGFlow 官方数据库。
- 验证：OpenAPI/JSON parse、Enterprise pytest、TypeScript、Vitest、P0 runner、RAGFlow guard。

## 2. Breaking Change Decision

选择 `/v2`。v1 已形成稳定仓库契约且公开内部 ID，但外部消费事实未知；未知不能作为允许破坏的证据。v1 保持兼容，新设备管理系统只使用 v2。迁移影响见 ADR-006。

## 3. Trust Boundary Decision

`CredentialIdentity = {credentialId, keyId, allowedBindings[{tenantId,sourceSystem}], status, validFrom, validUntil}`。授权对象是 binding pair，禁止用 tenant/source 两个集合做笛卡尔积。请求 tenant/source 只用于声明目标，必须与 credential binding 相交。

HMAC headers 固定为 `X-TY-Timestamp`、`X-TY-Key-Id`、`X-TY-Signature: v1=<hex>`。canonical string 为：signature version `v1`、timestamp、uppercase method、canonical path+sorted percent-encoded query、raw body SHA-256，各占一行。时钟窗口 ±300 秒，成功验签 replay key 保留 10 分钟，比较必须 constant-time。active key 可用；previous key 只有 24 小时 grace；revoked/expired key立即拒绝。replay protection 使用 Redis/Valkey `SET NX EX` 原子写入；生产环境 Redis 不可用时 fail closed，不得静默退回进程内内存。secret 永不出现在请求。v1 Bearer 仅兼容 v1。

Callback endpoint 只来自 server-side `sourceSystem -> endpoint` 配置，payload 禁止 callbackUrl。outbound callback 使用独立 credential。

## 4. Event Naming / Lifecycle Decision

采用现有主入口：`POST /documents`，`eventType` 只为 `upsert|reindex`。disable、restore、delete 继续使用独立 REST endpoint；不新增第二套 webhook 主入口。命令名不添加 `document.*` 前缀。

API envelope 固定 camelCase；`metadata` 固定 snake_case。`tenantId/sourceSystem/externalDocumentId/sourceVersionId` 与 metadata 同义字段必须相等，否则 422。

## 5. Idempotency Decision

业务 key 为 `(tenantId, sourceSystem, externalDocumentId, sourceVersionId)`；`eventId` 全局唯一且 lookup 仍受 credential binding 约束。normalized payload 是移除 `eventId`、`batchId` 后，对字段递归排序、字符串保持原值、SHA-256 转小写所得 canonical JSON 的 SHA-256。

- A：same eventId + same normalized hash -> 202，返回原 operation，`deduplicated=true`。
- B：same eventId + different normalized hash -> 409 `EVENT_ID_CONFLICT`。
- C：same business key + different eventId + same sha256 -> 202，返回已有资源，`deduplicated=true`。
- D：same business key + different sha256 -> 409 `DOCUMENT_VERSION_CONFLICT`。

首次与 replay 均为 202。reindex 必须是显式 `eventType=reindex`，不得利用 payload 差异绕过版本冲突。

## 6. Conversation Context Decision

创建会话可携带 initial context；创建后唯一持久 mutation 为 `PATCH /conversations/{id}/context`。context 字段为 `equipmentId/fixedAssetNo/faultCode`。省略=不变，null=清除；有效变化令 `contextVersion` +1。初始无 context 为 0，有 context 为 1。

`equipmentId` / `fixedAssetNo` 由 EAM 提交或从用户问题命中的文档 metadata 绑定，Gateway 原样保存，不调用 Asset Registry。文档 metadata 是问询筛选键，不是跨系统身份回查。

实际检索集合为 `ACL scope ∩（若已绑定则再交设备 metadata）∩ active/current version ∩ quality passed`。没有 canonical equipment 的 draft conversation 可以发送消息：能从问题抽出唯一已投喂设备号则绑定并筛选；否则在当前用户 ACL 可见文档内全局检索，并在回答末尾建议补充设备号。禁止先全库召回再删除。`faultCode` 只进入 server-side prompt/suggestion rules，不作为文档身份。

消息请求不接受 equipmentId/fixedAssetNo/faultCode override。一旦会话已绑定 equipmentId，首条消息后不可改绑；切换设备必须新建 conversation。尚未绑定前允许首句/后续句或 PATCH 完成首次绑定。

## 7. Pagination / Lifecycle Decision

会话列表使用 opaque cursor，默认 20、最大 100，稳定排序 `lastMessageAt DESC, conversationId DESC`。返回 `{items,nextCursor,hasMore}`。title 由服务端取首条 user message 规范化后截断 80 字符；没有消息时为 `New conversation`，不调用 LLM。

状态只有 `active|archived`。archive 后历史与 citation 可读，context 和消息写入返回 409 `CONVERSATION_ARCHIVED`。详情与消息分离；`GET /conversations/{id}` 只返回 summary/context，messages 使用 cursor+limit，排序 `createdAt ASC,messageId ASC`。

## 8. SSE / Message Idempotency Decision

消息入口为 `POST /conversations/{id}/messages`。`Accept: text/event-stream` 返回 SSE，否则 JSON。SSE 事件顺序为 `run.started` → 0..n × (`reasoning.delta` | `answer.delta`) → 0..1 × `answer.replaced` → 0..n × `citation` → `answer.completed` 或 `run.failed`。`answer.replaced` 表示丢弃先前流出的正文、改用本帧 `content`。已完成 run 的回放只发合并后的单帧 `reasoning.delta`/`answer.delta`，不发 `answer.replaced`。`answer.completed` 不含 `reasoning`。请求可带 `reasoningMode`（`simple|low|medium|high|ultra`，默认 `simple`）。URL 与既有字段名不变。

请求严格 oneOf：question branch `{clientMessageId,question}` 或 suggestion branch `{clientMessageId,suggestionId,contextVersion}`。同 conversation + clientMessageId + 同 normalized payload replay 原结果；payload 不同返回 409 `CLIENT_MESSAGE_ID_CONFLICT`。首次请求建立持久 run 状态机 `running -> completed|failed`、稳定 runId 和有限租约；租约过期只落为稳定 `RUN_INTERRUPTED`，不自动重跑。pending run replay 返回相同 runId/状态并使用 202 JSON，不重复插入 user message；SSE pending 也不得伪造流。

## 9. System Prompt Decision

客户端永不允许 `systemPrompt/hiddenPrompt/promptOverride/tools/toolDefinitions`。所有 request DTO strict，未知字段 422；OpenAPI request object 全部 `additionalProperties:false`。`promptProfile/promptVersion` 由 server rules/deployment/trusted admin 决定，可记录但不可由客户端修改。

## 10. Suggested Actions Decision

P0 为 server-side rule suggestions。GET 返回 `suggestionId,label,displayPrompt,contextVersion`，contextVersion 必填。点击只回传 suggestionId、clientMessageId、contextVersion；服务端重新加载 context/ACL/definition。版本不一致返回 409 `SUGGESTION_STALE`，不信任客户端 intent/arguments/equipmentId。

## 11. P0 / P1 / P2 Freeze

- P0：document ingestion/status/lifecycle、credential binding/HMAC/replay、idempotency A-D、conversation list/detail/messages/context/archive、retrieval context、clientMessageId、SSE/JSON、rule suggestions、strict DTO、server prompt、citation snapshot。
- P1：outbound callback、transient attachment (`indexPolicy=never`、conversation scoped、TTL)、只读 Business PG/TimeSeries adapter、hybrid fusion；v2.1 仅将 transient attachment 提升为本阶段正式公开能力，其余 P1 项仍为 planned。
- P2：LLM suggestions、advanced memory、advanced agent behavior。

唯一持久向量入口是 Document Ingestion Pipeline。attachments、DB/TimeSeries records、messages、summary 禁止持久 embedding。DB/TimeSeries 只允许 query-time read-only 白名单 adapter，不做 Text-to-SQL。

P1 callback payload 固定包含
`deliveryId/eventType/originatingEventId/externalDocumentId/sourceVersionId/status/timestamp/payloadVersion`。
TYrag 使用独立 outbound credential 向 server-side sourceSystem endpoint 签名投递；2xx
表示成功，其他状态或网络失败按 1/5/30/120/600 秒退避并最多尝试 8 次，之后进入
dead-letter。callback 失败只影响 delivery 状态，绝不回滚已经成功的 ingestion。

P1 attachment 固定 conversation scoped、`indexPolicy=never`、TTL 24 小时；过期后物理
删除临时对象和提取文本。它不得调用 Document Ingestion Pipeline，不创建持久
embedding，也不得跨会话复用。v2.1 正式公开 create、ticket、download 三个接口；
create/ticket 必须使用 User JWT，download ticket 是有界 bearer capability，可选 JWT
一旦提供就必须与 tenant/business user 一致。所有权、会话状态、TTL、下载次数、对象
完整性和失败清理均由 Gateway 服务端校验。

## 12. Current vs Required Matrix

| 能力 | 冻结前 | v2 要求 |
|---|---|---|
| v1 内部 ID | IMPLEMENTED_BUT_CONTRACT_MISMATCH | DEPRECATED（仅 v1 保留） |
| Credential binding/HMAC | MISSING | IMPLEMENTED（Redis/Valkey atomic SET NX EX；生产无静默内存 fallback） |
| 文档 status/lifecycle | IMPLEMENTED | IMPLEMENTED（外部 ID only） |
| idempotency conflict | MISSING | IMPLEMENTED |
| conversation create/detail/SSE/citation | IMPLEMENTED | IMPLEMENTED（Grounding 后安全下发） |
| list/messages cursor/archive | MISSING | IMPLEMENTED |
| context mutation/version | PARTIAL | IMPLEMENTED（Asset Registry snapshot/TTL/immutable equipment） |
| context retrieval filter | MISSING | IMPLEMENTED（无 context 不发送） |
| clientMessageId | MISSING | IMPLEMENTED |
| rule suggestions | MISSING | IMPLEMENTED |
| callback/adapters | PLANNED | PLANNED (P1) |
| transient attachment | PLANNED | IMPLEMENTED（v2.1；conversation scoped、TTL、`indexPolicy=never`） |
| LLM suggestions/advanced memory | OUT_OF_SCOPE | PLANNED (P2) |

## 13. OpenAPI Delta

新增 `integration-openapi-v2.yaml` 与 `/enterprise/api/v2` server；删除 v2 外部内部 ID；添加 HMAC headers、安全 binding 语义、document status scope、conversation list/context/messages/archive/suggestions、strict oneOf、cursor objects。v2.1 保持 `/enterprise/api/v2` 路径不变，以 additive minor version 正式公开 transient attachment create/ticket/download；callback 与其他 P1 项仍为 planned。v2.8 additive 增加 `Citation.refIndex`，供 EAM 将正文 `[ID:n]` 绑定到 `citations[]`（禁止用数组下标当 `n`）。

## 14. Docs / Task Rebaseline

`enterprise/web` 统一定位为 Integration Test Harness + Demo UI + Diagnostics UI。正式用户体验、业务导航和设备工作流由设备管理系统负责。WP-05 不再交付正式客户业务前端。

## 15. Follow-up Implementation Tasks

Owner、依赖、复杂度、并行性和 acceptance criteria 见 `tasks/P0-External-Integration-Contract-Rebaseline-v2.md` T0..T6。本轮 P0 实现不得顺带实现 P1/P2。

## 16. Modified Files

- 契约/计划：`contracts/integration-openapi-v2.yaml`、本冻结文档、
  `contracts/error-codes.yaml`、`contracts/ragflow-api-capability-matrix.md`、ADR-006、
  `tasks/P0-External-Integration-Contract-Rebaseline-v2.md`。
- 生产实现：`enterprise/gateway/app.py`、`auth/service_auth.py`、
  `auth/service_principal.py`、`sync/models.py`、`sync/sync_service.py`、
  `sync/v2_router.py`、`query/v2_store.py`、`query/v2_router.py`、
  `enterprise/gateway/asset_registry.py`。
- 自动化：`enterprise/scripts/run_enterprise_tests.ps1`、
  `enterprise/requirements-test.txt`、Enterprise v2 contract/runtime tests、独立 fixtures、
  live contract fixture path/cleanup、`enterprise/web/src/test-setup.ts`、
  Redis cross-instance integration test。
- 定位文档：`README.md`、`docs/00/01/02/05/07/09/11/12/13`、`tasks/WP-05`。

实际交付仍以当前 `git diff --name-only` 和 runner artifact 为准；用户原有
`contracts/wp04-phase3-contract-freeze.md` 不属于本任务。

## 17. Tests

Contract/P0/Integration/All profiles 和 C-01..C-20、E-01..E-16 见任务文档。Contract freeze 只由 Contract Gate 证明；implementation/integration 分别有独立 gate。

## 18. Remaining Open Questions

无影响本阶段接口实现的契约问题；本文件冻结 wire contract v2.1.0，但当前实现只是一体化候选基线，不是生产验收。候选仍需真实 Redis/Valkey 跨实例测试；真实设备管理系统 Asset Registry、对象存储和 RAGFlow 环境缺失时 Integration Gate 保持 exit 3。Gateway run/conversation/attachment metadata 状态仍使用 Enterprise 自有 SQLite，支持单 Gateway/多 worker 的候选部署；生产多副本 PostgreSQL repository、迁移、连接池和恢复机制另立任务，绝不使用客户业务 PG 或 RAGFlow 官方 DB 承载 Gateway 状态。

CONTRACT FROZEN
