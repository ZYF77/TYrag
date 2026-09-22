# EAM 文件复核入站接口说明（quality:confirm / disable）

版本：`v1`  
对应出站：`document.terminal` / `review_required`（`retrievable=false`）

本文给 EAM 联调：Gateway 质量门判 `review_required` 后，EAM 如何**确认放行**或**停用**，并映射到 RAGFlow 文档启停与授权集 G。

## 1. 鉴权

与现网投喂 / `disable` / `quality:reevaluate` 相同：**EAM → Gateway service principal / HMAC**。

- 需要有效的服务凭证（`ServiceAuth`）
- 不要用终端用户 Bearer 调这两个写接口

## 2. 确认放行 — `POST /enterprise/api/v1/documents/{externalDocumentId}/quality:confirm`

### 2.1 语义

| 当前状态 | 行为 |
|---|---|
| `parse_quality_status=review_required` 且为最新可晋升版本 | 审计覆盖写 `passed` → 走现有 `promote_quality_passed_version`（RAGFlow enable + `promote_version_if_latest`）→ 进入可检索 G |
| 已是 `passed` 且已是 current/可检索 | **幂等成功**（同结果，不重复副作用） |
| 非 latest | **409 CONFLICT**（不抢双 current） |
| `business_status=disabled` | **409 REVIEW_ALREADY_CLOSED**（禁止静默放行） |
| 缺少 actor/reason/source 字段 | **422 VALIDATION_ERROR** |
| RAGFlow 不可用 | **503 RAGFLOW_UNAVAILABLE**（可重试） |

> 不会改拒答文案；也不会只开 RF 却不 promote。

### 2.2 请求示例

```http
POST /enterprise/api/v1/documents/ATT-1229/quality:confirm
Idempotency-Key: DEC-20260922-001
Content-Type: application/json

{
  "tenant_id": "default",
  "source_system": "EAM",
  "source_version_id": "v1",
  "actor": "eam-reviewer-42",
  "reason": "扫描件人工复核通过，允许进入检索",
  "decision_id": "DEC-20260922-001"
}
```

说明：

- `source_system` / `source_version_id` 与 `quality:reevaluate` 对齐（确认的是哪一版）。
- `actor`、`reason` **强制非空**（审计）。
- `decision_id` 与头 `Idempotency-Key` 二选一必填（建议两者同值）。

### 2.3 成功响应示例

```json
{
  "externalDocumentId": "ATT-1229",
  "sourceVersionId": "v1",
  "parseQualityStatus": "passed",
  "currentVersion": 1,
  "businessStatus": "active",
  "promoted": true,
  "idempotent": false,
  "decisionId": "DEC-20260922-001",
  "actor": "eam-reviewer-42",
  "reason": "扫描件人工复核通过，允许进入检索",
  "requestId": "…"
}
```

## 3. 停用 — `POST /enterprise/api/v1/documents/{externalDocumentId}/disable`

复用现网停用语义，**不为 EAM 另造一套**。

```http
POST /enterprise/api/v1/documents/ATT-1229/disable?tenant_id=default&source_system=EAM
```

行为（与现网一致）：

- 将该外部文档下活跃版本标为 `business_status=disabled`
- RAGFlow 侧 disable（不可检索）
- **幂等**：已 disabled 再调仍成功

与出站 `review_required` 的关系：EAM 收到复核通知后，可选 **confirm（放行进 G）** 或 **disable（停用）**；二者互斥意图，disabled 后不可再 confirm。

## 4. 错误码对齐

| code | HTTP | 含义 |
|---|---|---|
| `NOT_FOUND` | 404 | 文档/版本不存在 |
| `VALIDATION_ERROR` | 422 | 缺字段或 actor/reason 为空 |
| `CONFLICT` | 409 | 非 latest、状态不可确认、promote 拒绝等 |
| `REVIEW_ALREADY_CLOSED` | 409 | 已 disabled，禁止放行 |
| `RAGFLOW_UNAVAILABLE` | 503 | RAGFlow 暂时不可用（可重试） |

（OpenAPI：`contracts/integration-openapi.yaml` 中 `quality:confirm` / `disable`。）

## 5. 与出站 `review_required` 的对应关系

1. Gateway 质量门 → `review_required` → 出站通知 EAM（`retrievable=false`，`current_version` 仍为 0 / 未 promote）。
2. EAM 人工确认 → 调 `quality:confirm` → `passed` + promote → 进入授权 G。
3. EAM 判定不可用 → 调 `disable` → 业务停用 + RF disable。

## 6. 非目标

- 不提供「只改 RF/business、不 promote」的旁路。
- 不复用历史 approve 接口硬套。
- Console 一键强制 passed 不在本接口范围。
