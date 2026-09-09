# RAGFlow 解析终态 Webhook 设计评估（Gateway 轮询 → 推送主路径）

> 日期：2026-09-04（Asia/Shanghai）  
> 范围：**设计 / 评估 only**；不实施大改、不合并 RF 大补丁、不 git commit/push。  
> 代码基准：本仓库 `VERSION=v0.26.4`；Gateway 在 `enterprise/gateway/**`；RAGFlow 在 `ragflow/**`。  
> 产品固定决策：
> 1. RAGFlow **不**直接通知 EAM；
> 2. Gateway→EAM 终态回调链路保持不变；
> 3. Gateway 轮询 RAGFlow 解析状态 → **改为 RAGFlow→Gateway webhook 为主路径**；
> 4. `StatusReconciler` 轮询保留为低频回填/对账，**暂不删除**。

---

## 1. 现状：StatusReconciler / 质检 / EAM 回调链

### 1.1 组件与启动接线

| 组件 | 文件 | 关键类型 / 函数 | 作用 |
|---|---|---|---|
| 生命周期启动 | `enterprise/gateway/app.py` `lifespan` | 后台 `asyncio.create_task` | 同时拉起 Outbox / StatusReconciler / Quality / Callback |
| 解析状态对账 | `enterprise/gateway/sync/worker.py` | `StatusReconciler` | 主轮询入口 |
| 状态刷新与副作用 | `enterprise/gateway/sync/sync_service.py` | `SyncService.refresh_status` | 读 RAGFlow `run` → 映射 → 质检入队 / 失败回调 |
| run→sync 映射 | `enterprise/gateway/sync/status_mapping.py` | `map_ragflow_run_to_sync_status` | 对齐 RF `TaskStatus` / `_process_run_mapping` |
| 状态机 | `enterprise/gateway/sync/state_machine.py` | `is_terminal_document_status` / `validate_transition` | 文档态转换门禁 |
| 质检入队 | `sync_service.py` | `_ensure_quality_evaluation` → `quality.models.get_or_create_evaluation` | `ready` 后幂等创建 evaluation + job |
| 质检执行 | `enterprise/gateway/quality/worker.py` | `QualityEvaluationWorker` / `QualityEvaluationService.run_job` | 评测、晋升版本、发终态回调 |
| 质检门禁语义 | `enterprise/gateway/quality/gate.py` | `enforce_quality_gate` | 查询侧门禁（与入库终态回调正交） |
| EAM 出站 | `enterprise/gateway/callback_delivery.py` | `enqueue_terminal_callback` / `emit_terminal_callback_safe` / `CallbackDeliveryWorker` | outbox + HMAC 投递 |
| 签名/幂等策略对象 | `enterprise/gateway/callback.py` | `sign_payload` / `verify_signature` / `CallbackIdempotencyLedger` / `classify_delivery` | 可复用到入站 webhook |
| 配置旋钮 | `enterprise/gateway/config.py` + `runtime_settings.py` | `reconcile_seconds`（默认 **10s**）、`status_reconciler_enabled`、`callback_*`、`quality_*` | 运行时开关 |

启动片段（`app.py`）：`OutboxWorker`、`StatusReconciler`、`QualityEvaluationWorker`、`QualityReconciler`、`CallbackDeliveryWorker` 在非 test 模式一并启动。

### 1.2 StatusReconciler 行为（当前主路径 = 轮询）

`StatusReconciler.run_forever`：

1. 若 `runtime_settings().status_reconciler_enabled`，调用 `run_once`；
2. sleep `reconcile_seconds`（env `ENTERPRISE_RECONCILE_SECONDS`，默认 10.0）。

`run_once`：

1. `list_mappings(statuses=IN_PROGRESS_STATUSES)`，其中  
   `IN_PROGRESS_STATUSES = {registered, queued, parsing, indexing, validating, review_required, tracking}`；
2. 另取 `ready` 且 `pipeline_status` **非**终态（`DONE/3/FAIL/4/CANCEL/2`）的映射，继续刷新（覆盖 GraphRAG/RAPTOR 等特殊任务窗口）；
3. 对每条调用 `SyncService.refresh_status(doc)`；
4. 再调用 `reconcile_missing_ragflow_documents()`（ready/review_required 文档在 RF 侧消失 → `mark_ragflow_document_missing`）。

### 1.3 `refresh_status` → 质检 / 失败回调

`SyncService.refresh_status`（`sync_service.py`）：

1. 跳过无 `ragflow_dataset_id`/`ragflow_document_id`，或 `superseded/disabled/deleted`；
2. `ragflow_client.list_documents(dataset_id, document_id=...)` 读回 `run`；
3. `map_ragflow_run_to_sync_status(run)`：
   - `DONE/3` → `ready`
   - `FAIL/4` → `failed`
   - `RUNNING/1` → `parsing`
   - `CANCEL/2` → `cancelled`
   - `UNSTART/0` / 未知 → `registered`（保守非终态）
4. 若 mapped ∈ `{ready, failed}`，先走 `_retry_technical_parse_once`（空结果 DONE 或 FAIL 可技术重试一次）；
5. **ready**：`_set_status(... ready ...)` → **`_ensure_quality_evaluation(doc)`**（不直接回调 EAM）；
6. **failed**：写错误字段 → **`_emit_terminal_failed_if_no_quality(doc)`** → `emit_terminal_callback_safe(..., "failed", ...)`；
7. 中间态：允许则 `_set_status`；
8. RF 读不到文档 → `mark_ragflow_document_missing`。

### 1.4 质检 → Gateway→EAM 终态回调（保持不变）

成功路径：

1. `get_or_create_evaluation`（唯一键含 `evaluation_version≈processing_round`）创建 job；
2. `QualityEvaluationWorker.run_job` → `_evaluate` → `evaluate_document_quality`；
3. `passed` 时 `promote_quality_passed_version`（启用当前版本、禁用旧版本）；
4. `_emit_terminal_callback`：
   - `passed` + retrievable → `terminal_status="retrievable"`
   - `passed` 但未可检索 → `review_required`
   - `review_required` / `failed` → 对应终态；
5. `enqueue_terminal_callback`：`ON CONFLICT (tenant_id, source_system, external_document_id, source_version_id, processing_round, terminal_status) DO NOTHING`（表 `callback_delivery`，约束名 `uq_callback_delivery_round_terminal`）；
6. `CallbackDeliveryWorker` HMAC 签名后 HTTP POST 到 `ENTERPRISE_CALLBACK_ENDPOINTS`（EAM）。

**关键约束（产品）**：RAGFlow 永不直连 EAM；EAM 只吃 Gateway 的 `document.terminal`（`CALLBACK_EVENT_TYPE`）出站 envelope。

### 1.5 责任边界示意

```
EAM ──POST /documents──► Gateway OutboxWorker ──upload/parse──► RAGFlow
                              │                                    │
                              │  StatusReconciler（现状轮询）         │ task_executor
                              │  ◄── list_documents(run) ───────────┘
                              │
                              ├─ ready → QualityEvaluationWorker
                              │              │
                              │              └─ emit_terminal_callback_safe
                              │                        │
                              └─ failed(无质检) ─────────┤
                                                       ▼
                                              CallbackDeliveryWorker ──HMAC──► EAM
```

目标改造后：上图「StatusReconciler 轮询」降为低频回填；主信号改为 RAGFlow→Gateway webhook。

---

## 2. RAGFlow v0.26.4 最佳挂钩点

### 2.1 文档 `run` 终态如何产生（vendored 代码）

| 机制 | 位置 | 行为 |
|---|---|---|
| TaskStatus 枚举 | `ragflow/common/constants.py` `TaskStatus` | `UNSTART=0 RUNNING=1 CANCEL=2 DONE=3 FAIL=4` |
| 公开 API 映射 | `api/apps/services/document_api_service.py` `_process_run_mapping` | 数字 → `UNSTART/RUNNING/...` 字符串（Gateway `status_mapping.py` 已对齐） |
| **DONE 聚合写库** | `DocumentService._sync_progress`（`document_service.py` ~1085–1157） | 聚合该 doc 全部 task：全部完成且无 bad → `run=DONE`；有 bad → `run=FAIL`；否则 `RUNNING` |
| 周期驱动 DONE | `api/ragflow_server.py` `update_progress()` | Redis 锁内每 ~**6s** 调 `DocumentService.update_progress()` |
| **FAIL 即时写库** | `TaskService.update_progress`（`task_service.py` ~424–429） | `progress == -1` 时直接把 Document `run=FAIL` |
| 进度回调 | `rag/svr/task_executor.py` `set_progress` | 调 `TaskService.update_progress`；成功终帧常 `prog=1.0`，失败 `prog=-1` |
| 立即同步入口 | `DocumentService.update_progress_immediately` | 少量路径（如 pipeline log）会立刻 `_sync_progress` |
| 特殊任务冻结 | `_sync_progress` + `begin2parse(keep_progress=True)` | GraphRAG/RAPTOR/Mindmap 可在 doc 已 DONE 时继续跑；Gateway 已用「ready + 非终态 pipeline」续刷 |

**重要延迟**：经典路径下 task `progress=1.0` **不等于** document `run=DONE`；DONE 往往要等 `ragflow_server` 的 6s 周期 `_sync_progress`。Webhook 若挂在 task 完成瞬间，可能早于公开 `run=DONE`，与 Gateway 当前语义不一致。

### 2.2 推荐挂钩点（按优先级）

| 优先级 | 挂钩点 | 触发终态 | 理由 |
|---|---|---|---|
| **P0 主钩** | `DocumentService._sync_progress`：在即将 `model.update(info)` 且 **`run` 从非终态变为 `DONE`/`FAIL`**（可选含 `CANCEL`）时发出 | DONE / FAIL（及 CANCEL） | **文档级权威终态**；与 Gateway `map_ragflow_run_to_sync_status` / 公开 list API 一致；天然避开「单 task 完成但多 task 未齐」 |
| **P0 辅钩** | `TaskService.update_progress`：`progress==-1` 写 Document FAIL **成功之后** | FAIL | FAIL 多数走即时路径，不经过等待 6s 的 `_sync_progress`；避免 FAIL 漏推 |
| P1 可选加速 | task 全部 `progress>=1` 后显式调用 `update_progress_immediately([doc])`，再由主钩发事件 | DONE 更快 | 缩小「task done → doc DONE」窗口；改动面略大，可第二阶段 |
| **不推荐单独作为主钩** | 仅 `task_executor.set_progress(prog=1.0)` / `handle_task` finally | 伪 DONE | 多 task、特殊任务、chunk_num 时序（见 `task_executor_refactor/dataflow_service.py` 注释）会导致早发或错发 |

### 2.3 本仓库是否已有可复用 RF hook / webhook？

**结论：没有可直接复用的「文档解析终态 → HTTP webhook」补丁。**

| 已有 RF 相关 | 说明 | 与本需求关系 |
|---|---|---|
| RF-PATCH-001~006 | compose / empty_response / grounding / websearch / 临时附件等 | 无关 |
| **RF-PATCH-007** `CHANGE-REQUEST-RF-PATCH-007-COMPLETION-STATUS.md` | completion **问答** `status`（`completed` / `no_reliable_evidence` / `failed`） | **不是**文档 parse `run`；不可复用为解析 webhook |
| RF-PATCH-008/009 | 可观察性 / RAG 诊断 | 无关 |
| `enterprise/gateway/callback.py` | HMAC、时间窗、幂等 ledger、重试分类 | **Gateway 侧入站验签可对齐复用**；不是 RF 出站 |
| `callback_delivery.py` | Gateway→EAM outbox | 出站终态；RF 不调用 |
| 协议/计划文档 | 「Webhook deferred / 本阶段不实施」针对的是 **EAM↔Gateway** 文档登记推送，不是 RF→Gateway | 历史决策不阻止本内部 webhook；但需在文档中区分两条链路 |
| Audit `AUDIT-REPORT-TYRAG-V0264.md` | 建议「评估用 RAGFlow webhook 替代轮询」 | 与本评估一致；其中「需改造 EAM 回调」按产品决策 **不必**——EAM 回调保持 Gateway 出站 |

### 2.4 建议的 RF-PATCH 形态（仅设计，不实施）

- 新补丁号预留：**RF-PATCH-010**（名称建议：`document-run-terminal-webhook`）。
- 最小改动面：
  1. 新增纯函数模块，例如 `ragflow/api/utils/enterprise_status_webhook.py`：组装 payload、HMAC 签名、带超时的 HTTP POST、本地失败日志（**禁止**在 RF 内实现 EAM 语义）。
  2. `_sync_progress` 与 `TaskService.update_progress(FAIL)` 两处调用「fire-and-forget / 短超时」发送。
  3. 配置：`ENTERPRISE_STATUS_WEBHOOK_URL`、`ENTERPRISE_STATUS_WEBHOOK_SECRET`、`ENTERPRISE_STATUS_WEBHOOK_ENABLED`、超时/最大重试（RF 侧轻量重试即可；最终一致性靠 Gateway reconciler）。
- **不要**在 RF 内查 Gateway DB 或直接调 quality/callback。

---

## 3. 最小事件契约草案（RF → Gateway）

对齐本仓库已有 `callback.py` 习惯（`timestamp + raw body` HMAC、`sha256=`、300s 窗）及业界 Stripe/Standard Webhooks 模式。

### 3.1 事件名

- 推荐：`ragflow.document.run.terminal`
- 仅在文档 `run` 进入 **`DONE` / `FAIL` / `CANCEL`** 时发送（中间态 `RUNNING` 默认不发，降低噪声；若 Leader 要进度条可另开 `ragflow.document.run.progress`，非 P0）。

### 3.2 请求

- **方法 / 路径（Gateway 接收）**：  
  `POST /enterprise/api/v1/internal/ragflow/document-run-terminal`  
  （备选：`/enterprise/api/v1/webhooks/ragflow/document-run`）  
  前缀强调 **internal**，与对外 FILE_SHARE / EAM 契约隔离。
- **Headers**：
  - `Content-Type: application/json`
  - `X-Enterprise-Timestamp: <unix_seconds>`
  - `X-Enterprise-Signature: sha256=<hex>`（对 `f"{timestamp}." + raw_body` 做 HMAC-SHA256，与 `sign_payload` 一致）
  - `X-Enterprise-Event-Id: <idempotency key>`（亦写入 body，双写防丢）
- **Body（JSON，字段稳定、snake 或 camel 选定一种；建议 camel 与现有 callback envelope 一致）**：

```json
{
  "schemaVersion": "rf.status.v1",
  "eventId": "01J…或 uuid",
  "eventType": "ragflow.document.run.terminal",
  "occurredAt": "2026-09-04T02:00:00.000Z",
  "payload": {
    "ragflowDocumentId": "<doc.id>",
    "ragflowDatasetId": "<doc.kb_id>",
    "run": "DONE",
    "runCode": "3",
    "progress": 1.0,
    "progressMsgTail": "…可选截断…",
    "trigger": "sync_progress|task_fail",
    "enterpriseEventId": "<meta_fields.enterprise_event_id 若有>"
  }
}
```

### 3.3 必填字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `eventId` | 是 | 幂等键；**稳定生成**优先：`sha256(doc_id + "|" + runCode + "|" + doc.update_time)`；若无可靠 update_time 则用 UUID，但须 RF 出站 outbox 持久化同一 id 重试 |
| `eventType` | 是 | 固定上值 |
| `payload.ragflowDocumentId` | 是 | Gateway `ext_document_map.ragflow_document_id` 查找键 |
| `payload.ragflowDatasetId` | 是 | 校验绑定，防串库 |
| `payload.run` / `runCode` | 是 | `DONE/3` `FAIL/4` `CANCEL/2` |
| `occurredAt` | 是 | UTC ISO8601 |
| `payload.enterpriseEventId` | 否 | 若 meta 有则带上，辅助排障 |
| `payload.trigger` | 否 | 观测用 |

### 3.4 幂等键

- **主幂等键**：`eventId`（Inbox 表唯一约束）。
- **业务去重辅助键**（Gateway 处理侧）：`(ragflow_document_id, runCode, processing_round)` —— 同一轮同一终态重复投递不重复入质检 / 不重复失败回调。
- 已有 EAM 侧去重：`callback_delivery` 的 `uq_callback_delivery_round_terminal` 继续兜底。

### 3.5 签名

- 复用 `enterprise.gateway.callback.sign_payload` / `verify_signature`（`max_age_seconds=300`）。
- 密钥：独立于 EAM 出站密钥，例如 `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_SECRET`（RF 与 Gateway 共享）；**禁止**复用用户 JWT / RAGFlow API Key 当 HMAC 秘密（可另加可选 Bearer 做网络层 ACL）。

### 3.6 重试语义（发送方 RF）

| Gateway HTTP | RF 行为 |
|---|---|
| 2xx | 成功，停止重试 |
| 401/403（验签失败） | **不重试**（配置错误；打错误日志） |
| 400（schema） | 不重试 |
| 404（文档映射不存在） | 可短重试 1–2 次后放弃（可能 Gateway 尚未写完 mapping；最终靠 reconciler） |
| 408/429/5xx / 网络错误 | 指数退避重试（建议最多 5–8 次，与 `classify_delivery` 同思路）；超限丢给 reconciler 兜底 |
| RF 进程崩溃 | 无强 outbox 时依赖 Gateway `StatusReconciler` 回填（故 P0 **保留** reconciler） |

P1+ 可在 RF 增加轻量 **outbound outbox 表**（与 Gateway `callback_delivery` 同构），但 **非 P0 必需**。

### 3.7 Gateway 接收路径建议

- 新路由模块：`enterprise/gateway/sync/ragflow_status_webhook.py`（或 `sync/status_webhook_router.py`）。
- `app.py` `include_router`。
- Handler 伪流程：
  1. 读 raw body → `verify_signature`；
  2. 解析 JSON → 校验 schema；
  3. `INSERT INTO ragflow_status_inbox(event_id, …) ON CONFLICT DO NOTHING`；冲突则 **200 + replay**（不二次副作用）；
  4. 按 `ragflow_document_id`（+ dataset_id）查 `ext_document_map`；
  5. 调用抽取后的 `SyncService.apply_ragflow_run(doc, run)`（与 `refresh_status` 共享核心，避免分叉）；
  6. 快速 200（若需更强隔离：inbox 标记 pending，后台 worker 处理；P0 可同步调用现有逻辑，因其已异步化质检/回调）。

---

## 4. Gateway 接收后如何接入质检 + EAM（防双发 / 防漏发）

### 4.1 接入点

**不要**新写一套「webhook → EAM」。应：

1. 将 `refresh_status` 中「已知 `run` 后的状态机 + 副作用」抽成  
   `apply_ragflow_run(doc, run: str, *, source: Literal["poll","webhook"])`；
2. Webhook handler 与 `StatusReconciler` **共用**该函数；
3. 副作用保持原样：
   - `ready` → `_ensure_quality_evaluation`（`get_or_create_evaluation` 已幂等）；
   - `failed` → `_emit_terminal_failed_if_no_quality` → `emit_terminal_callback_safe`（`callback_delivery` 唯一约束幂等）；
   - 技术重试 `_retry_technical_parse_once` 保留（空 DONE / FAIL）。

### 4.2 防双发（webhook + 轮询同时存在）

| 风险 | 既有 / 设计中的防护 |
|---|---|
| 两次进入 `ready` 各入一次质检 | `get_or_create_evaluation` `ON CONFLICT` |
| 质检完成两次回调 EAM | `uq_callback_delivery_round_terminal` |
| 失败回调两次 | 同上 + `emit_terminal_callback_safe` 不抛错吞掉冲突 |
| webhook 与 poll 交错把状态抖动 | `validate_transition` / `transition_allowed`；终态后 poll 再写同态应 no-op |
| 乱序：先收到 DONE webhook，后收到过期 RUNNING | **忽略非「前进」或非法转换**；inbox 可存 `occurredAt`，若 `occurredAt` 早于 mapping 已应用事件则丢弃 |
| 同一文档新 `processing_round` | 回调唯一键含 `processing_round`；旧轮终态不影响新轮 |

### 4.3 防漏发

| 机制 | 角色 |
|---|---|
| RF 轻量重试 | 主路径尽力送达 |
| **StatusReconciler 低频保留** | 兜底：RF 宕机、网络分区、401 配错修复后、多副本漏钩 |
| `reconcile_missing_ragflow_documents` | 文档被删场景（webhook 可不覆盖 delete；删除仍靠 poll / 现有 delete API） |
| 质检 `QualityReconciler` | 已入队但 worker 中断的回补（与本改无关，保持） |

### 4.4 明确不变

- Gateway→EAM 的 payload（`build_terminal_payload`）、HMAC 出站、endpoint 配置、启用开关 `callback_enabled`：**不变**。
- RAGFlow **零** EAM URL 配置。

---

## 5. 变更面与风险

### 5.1 变更面

| 层 | 变更 | 规模 |
|---|---|---|
| RF 新补丁 RF-PATCH-010 | `_sync_progress` + FAIL 路径 hook + 小模块 + env | **小到中**；需进 `patches/manifest.yaml` 与升级说明 |
| Gateway 入站路由 + inbox 表 | 新表 `ragflow_status_inbox`；router；`apply_ragflow_run` 抽取 | **中** |
| Gateway 配置 | webhook secret/url（RF 侧）、接收开关、reconcile 拉长 | 小 |
| EAM / 对外协议 | **无** | — |
| 删除 StatusReconciler | **明确不做（本阶段）** | — |

### 5.2 配置旋钮（建议）

**RAGFlow**

- `ENTERPRISE_STATUS_WEBHOOK_ENABLED`（bool，默认 false）
- `ENTERPRISE_STATUS_WEBHOOK_URL`（Gateway 完整 URL）
- `ENTERPRISE_STATUS_WEBHOOK_SECRET`
- `ENTERPRISE_STATUS_WEBHOOK_TIMEOUT_MS`（建议 1000–3000）
- `ENTERPRISE_STATUS_WEBHOOK_MAX_ATTEMPTS`

**Gateway**

- `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_SECRET`（与上对称）
- `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_ENABLED`（接收总开关）
- 既有 `ENTERPRISE_RECONCILE_SECONDS` / `status_reconciler_enabled`（P1 调大间隔，不关）

### 5.3 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| **多副本 task_executor / 多 api 进程** 重复发 webhook | 高 | `eventId` 稳定键 + Gateway inbox 唯一；发送侧也可进程内短 debounce |
| **乱序 / 过期事件** | 中 | 校验 transition；比较 `occurredAt` / `update_time` |
| **DONE 早于 chunk_num 可见** | 中 | 保持 `_retry_technical_parse_once` 空结果重试；钩在 `_sync_progress` 且 refactor 路径已先 `increment_chunk_num` |
| **特殊任务（GraphRAG 等）** | 中 | 仅在 `run` **值变化进入终态**时发；已是 DONE 再次 `_sync_progress` 不重复发；Gateway 对 ready+非终态 pipeline 仍靠 reconciler |
| **技术重试窗口** 误把临时 FAIL 当业务失败回调 | 中 | 先走 `_retry_technical_parse_once`，与今日 poll 语义一致 |
| **签名时钟偏斜** | 低 | 300s 窗；NTP |
| **密钥泄露 / 配错** | 中 | 独立 secret；失败不 5xx 盲重试；审计日志 |
| **RF 补丁升级冲突** | 中 | 集中在 `_sync_progress` / `update_progress` 末尾，manifest `upgrade_notes` 写清 |
| **Webhook 阻塞 task 线程** | 中 | 短超时 + 线程池/异步；失败不影响 parse 成功写库 |
| **与「EAM Webhook deferred」文档表述混淆** | 低 | 本设计是 **RF→Gateway 内部**；对外仍是 Gateway→EAM 既有 callback |

---

## 6. 分阶段落地

### P0 — 契约 + 接收端 + RF 挂钩（主路径切换能力）

1. 冻结本节事件契约（字段、签名、幂等、HTTP 语义）。
2. Gateway：inbox 表 + 接收路由 + `apply_ragflow_run` 与 `refresh_status` 合流；单测（验签、replay、乱序、映射缺失）。
3. RF-PATCH-010：两处 hook + 配置默认 **关闭**；预发环境打开做对比。
4. **StatusReconciler 保持默认 10s**（双轨）：对比「仅 webhook」与「webhook+poll」指标后再降频。
5. 验收：文档 parse DONE/FAIL 后质检入队与 EAM 回调与改造前一致；故意停 RF webhook 时 reconciler 仍能收敛。

### P1 — 降低轮询频率

1. `reconcile_seconds` 提到 60–300s（runtime settings 可配）；保留 enable 开关。
2. 可选：RF 侧 DONE 加速（`update_progress_immediately`）。
3. 仪表：webhook 成功率、inbox replay 率、reconciler「实际修正」计数（应为单调下降）。

### P2 — 可观测性与清理

1. 结构化日志 / metrics：`webhook_received_total{run,result}`、`webhook_signature_fail`、`reconciler_catchup_total`。
2. 评估 RF 出站 outbox；评估是否可进一步关闭 reconciler（**需单独决策，不是默认**）。
3. 文档：更新对接协议「内部状态信号」章节；与历史「EAM Webhook deferred」表述解耦。
4. 清理临时代码与双轨特性开关。

---

## 7. 明确「现在不实施」边界

本评估 **不**做：

1. 任何大段业务代码合入 / RF 大补丁 merge；
2. git commit / push；
3. 删除或默认关闭 `StatusReconciler`；
4. RAGFlow 直连 EAM 或把 `callback_delivery` 搬进 RF；
5. 改变 Gateway→EAM 终态 payload / 签名 / 唯一键语义；
6. 用 webhook 替代 FILE_SHARE 登记入口或 EAM→Gateway 文档 API；
7. 实现进度类高频 `RUNNING` 推送；
8. 处理 RF 文档物理删除的推送（仍靠现有 poll/API）；
9. 扩大 RF-PATCH-007（completion status）范围；
10. 多区域密钥轮换 UI / 完整 RF outbox 产品化（可留 P2）。

---

## 8. 开放问题（呈 Leader）

1. **CANCEL 是否要 webhook？** 当前 Gateway 会映射 `cancelled`；FILE_SHARE 主路径是否关心？建议 P0 发送但 Gateway 可配置忽略。
2. **稳定 `eventId` 是否强制基于 `(doc_id, run, update_time)`？** 若 `_sync_progress` 高频同态更新导致 update_time 变而 run 不变——主钩应只在 **run 值变化** 时发送，避免 eventId 膨胀。
3. **P0 是否要求 RF 出站 outbox？** 建议否（靠 reconciler）；若生产网络极差可升为 P0.5。
4. **接收路由鉴权**：仅 HMAC，还是 HMAC + mTLS / 内网 ACL？
5. **多 tenant 单 Gateway**：secret 全局一份是否足够，还是按 RF 部署实例区分？
6. Audit 报告写「需改造 EAM 回调」——请 Leader 确认对外口径统一为：**不改造 EAM**，只改 RF→Gateway。

---

## 9. 参考（仓库内）

- `enterprise/gateway/sync/worker.py` — `StatusReconciler`
- `enterprise/gateway/sync/sync_service.py` — `refresh_status` / `_ensure_quality_evaluation` / `_emit_terminal_failed_if_no_quality`
- `enterprise/gateway/quality/worker.py` — `_emit_terminal_callback`
- `enterprise/gateway/callback_delivery.py` / `callback.py` / `db/tables.py` `callback_delivery`
- `enterprise/gateway/sync/status_mapping.py`
- `ragflow/api/db/services/document_service.py` — `_sync_progress`
- `ragflow/api/db/services/task_service.py` — `update_progress` FAIL 分支
- `ragflow/rag/svr/task_executor.py` — `set_progress`
- `ragflow/api/ragflow_server.py` — 6s progress 循环
- `patches/CHANGE-REQUEST-RF-PATCH-007-COMPLETION-STATUS.md`（勿与 parse webhook 混淆）
- `docs/设备管理系统联调可运行版本实施计划.md` §1.3（EAM Webhook deferred——不同链路）

---

*文档状态：设计评估；无实现、无提交。*
