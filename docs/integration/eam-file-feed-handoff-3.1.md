# EAM 文件投喂对接说明（FILE_SHARE 3.1.0）

面向：EAM 开发 / 测试 / 运维  
契约版本：`FILE_SHARE API v3.1.0` + `FILE_SHARE callback v1.0.0`  
正式契约文件：

- `contracts/file-share-v3.yaml`
- `contracts/file-share-callback-v1.yaml`
- `contracts/metadata-schema.json`

测试环境回调接口（EAM 提供）：[`eam-feed-callback.md`](./eam-feed-callback.md)

EAM **只**对接 Enterprise Gateway，不要直接访问 RAGFlow、数据库、Redis、对象存储管理端口或 Gateway 内部 ticket 接口。

---

## 1. 本次变更范围（相对原先 statusUrl 轮询）

| 项 | 变更前 | 变更后（3.1.0） |
|---|---|---|
| 登记成功响应 | `202` + 完整 `DocumentStatus`（含 `statusUrl`、进度字段） | `202` + **瘦身受理回执**（无 `statusUrl`、无进度/质量字段） |
| 获知处理结果 | EAM 轮询 `statusUrl` | **Gateway 主动回调** EAM 终态接口 |
| 终态通知 | 无 | `retrievable` / `failed` / `review_required` |
| GET status | 主路径 | **仅诊断/运维**，正式对接不依赖 |
| 登记请求体 | 无 callbackUrl | **仍然禁止** `callbackUrl`；回调 URL 由双方运维在 Gateway 侧配置 |
| RAGFlow | 无直接对接 | **仍无**；Gateway 内部轮询 RAGFlow，汇总后再推 EAM |

**不变：**

- 仍先写共享盘 PDF，再 HMAC 登记坐标（不上传二进制）
- 登记 inbound HMAC（`X-TY-*`，`v1=` 签名）算法不变
- `tenantId` / `sourceSystem` / `storageRootId` 等联调参数约定不变

---

## 2. 总体流程

```text
EAM 写 PDF 到共享目录（原子改名）
  → HMAC POST /enterprise/api/v3/documents
  → Gateway 立即 202（已受理）
  → Gateway 后台：取文件 → RAGFlow 解析/索引 → 质量门
  → Gateway POST 终态回调到 EAM
  → EAM 按 status 更新业务状态（可检索 / 失败 / 待复核）
```

说明：

- `202` **不是**解析完成。
- EAM **不必**再轮询 Gateway。
- 回调失败由 Gateway 重试；**不会**因此回滚已入库状态。

---

## 3. 当前接口规范（EAM 调用我方）

### 3.1 鉴权（登记 / 诊断 GET）

每个请求必须带：

```http
X-TY-Timestamp: <10 位 Unix 秒>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<小写 hex HMAC-SHA256>
```

签名原文固定 5 行（末行后无多余换行）：

```text
v1
<timestamp>
<大写 HTTP method>
<规范化 path + 排序后的 RFC3986 query>
<最终 raw body 的 SHA-256；GET 时为空 body 的 SHA-256>
```

```text
signature = HMAC-SHA256(inboundSecret, UTF-8(canonicalText))
```

注意：

- 修改 JSON 空格/字段顺序后必须对**最终 raw body**重新签名
- 每次重试换新 timestamp/signature；相同业务可保留同一 `eventId`
- 不要用 `Authorization: Bearer`，不要用 RAGFlow API key

联调参数（测试环境示例，正式以安全渠道交付为准）：

| 参数 | 测试示例 | 说明 |
|---|---|---|
| Gateway | `http://192.168.30.30:5188` | 根地址 |
| tenantId | `wp04e2e` | 与 binding / metadata 一致 |
| sourceSystem | `EAM` | 固定 |
| keyId | `device-local-key` | 请求头 |
| storageRootId | `device-share` | 共享根逻辑名 |
| inbound HMAC secret | 单独交付 | 勿写入文档/前端/Git |

### 3.2 登记接口（正式主路径）

```http
POST {BASE_URL}/enterprise/api/v3/documents
Content-Type: application/json
Accept: application/json
X-TY-Timestamp: ...
X-TY-Key-Id: ...
X-TY-Signature: v1=...
```

**请求体要点**（完整 schema 见契约）：

| 字段 | 要求 |
|---|---|
| `eventId` | 必填；同一业务重试保持不变 |
| `eventType` | `upsert` 或 `reindex` |
| `tenantId` / `sourceSystem` | 与 binding 一致；`sourceSystem=EAM` |
| `externalDocumentId` | EAM 文档永久 ID |
| `sourceVersionId` | 文档版本；内容变必须换新版本和新路径 |
| `sha256` | PDF 实际字节 SHA-256（64 hex） |
| `fileName` | 纯文件名，不含 `/` `\` |
| `mediaType` | 固定 `application/pdf` |
| `source.kind` | 固定 `FILE_SHARE` |
| `source.storageRootId` | 交付的逻辑根，如 `device-share` |
| `source.relativePath` | 相对共享根的相对路径，禁止绝对路径/`..` |
| `source.size` | 建议填实际字节数 |
| `source.etag` | **当前请勿传** |
| `metadata.*` | 身份字段必须与顶层一致；勿加未定义字段 |
| `callbackUrl` | **禁止** |

身份一致性（必须）：

```text
metadata.tenant_id            = tenantId
metadata.external_document_id = externalDocumentId
metadata.source_system        = sourceSystem
metadata.document_version     = sourceVersionId
```

**成功响应 `202`（瘦身受理回执）：**

```json
{
  "operationId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "deduplicated": false,
  "updatedAt": "2026-08-13T02:00:00+00:00"
}
```

| 字段 | 含义 |
|---|---|
| `operationId` | 受理操作 ID（通常等于本次 `eventId`） |
| `externalDocumentId` | 文档 ID |
| `sourceVersionId` | 版本 ID |
| `deduplicated` | `true` 表示幂等重放/重复登记 |
| `updatedAt` | 服务端更新时间 |

响应中 **没有** `statusUrl` / `status` / `retrievable` / `qualityStatus` 等字段。

### 3.3 诊断状态（非正式主路径，可选）

运维排查可用：

```http
GET {BASE_URL}/enterprise/api/v3/documents/{externalDocumentId}/status?tenantId=...&sourceSystem=EAM&sourceVersionId=...
```

仍需 HMAC。正式业务状态请以 **回调 `status`** 为准。

---

## 4. EAM 需要新增的接口（我方回调你们）

### 4.1 职责

| 方向 | 提供方 | 调用方 | 作用 |
|---|---|---|---|
| 登记 | Gateway（我方） | EAM | 投喂登记 |
| **终态回调** | **EAM（对方新增）** | Gateway | 推送处理终态 |

当前测试环境回调地址（EAM 已提供，详见 [`eam-feed-callback.md`](./eam-feed-callback.md)）：

```http
POST http://192.168.30.31:5105/api/v1/ai/feed/callback
```

Gateway 按字符串 URL 形态配置（不要写成带 `keyId` 的对象；EAM 当前不校验 `X-TY-Key-Id`）：

```text
ENTERPRISE_CALLBACK_ENDPOINTS={"EAM":"http://192.168.30.31:5105/api/v1/ai/feed/callback"}
ENTERPRISE_CALLBACK_HMAC_SECRET=<outbound-secret>
ENTERPRISE_CALLBACK_ENABLED=true
```

登记 JSON **不要**带 `callbackUrl`。outbound secret **独立于** 登记 inbound HMAC（`device-local-key` / `v1=`）。

### 4.2 回调请求（Gateway → EAM）

```http
POST http://192.168.30.31:5105/api/v1/ai/feed/callback
Content-Type: application/json
X-TY-Timestamp: <10 位 Unix 秒>
X-TY-Signature: sha256=<hex>
X-TY-Key-Id: <可选，EAM 当前不校验>
```

**验签（与登记 inbound 不同）：**

```text
signed = "{timestamp}." + canonical_json_body
expected = "sha256=" + hex(HMAC-SHA256(outboundSecret, UTF-8(signed)))
```

- `canonical_json_body`：对 JSON 对象做 **key 排序**、无多余空格的紧凑序列化后的字节
- 建议校验时间窗（例如 ±300 秒）
- outbound secret **独立于** 登记 inbound secret

**Body 示例：**

```json
{
  "deliveryId": "a1b2c3d4-....",
  "eventType": "document.terminal",
  "originatingEventId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "status": "retrievable",
  "timestamp": "2026-08-13T02:05:00+00:00",
  "payloadVersion": "1",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "qualityStatus": "passed",
  "retrievable": true,
  "error": null
}
```

| 字段 | 说明 |
|---|---|
| `deliveryId` | 投递唯一 ID；EAM 应按此做幂等 |
| `originatingEventId` | 对应登记时的 `eventId` / `operationId` |
| `status` | **仅三值**：`retrievable` / `failed` / `review_required` |
| `retrievable` | 成功终态为 `true`；否则 `false` |
| `qualityStatus` | `passed` / `review_required` / `failed` / `unknown` / `null` |
| `error` | `failed` / `review_required` 时必填语义：`{code,message,retryable[,reasonCodes]}`；`retrievable` 为 `null` |

**EAM 应如何处理 `status`：**

| status | 业务含义 | EAM 建议动作 |
|---|---|---|
| `retrievable` | 已解析且质量通过，已提升为可检索版本 | 标记文档可问答/已入库成功 |
| `failed` | 处理失败（源文件、哈希、解析、质量失败等） | 标记失败；读 `error.code` / `error.message` |
| `review_required` | 质量门要求人工复核；**不会**自动变成可检索 | 进入复核；读 `error`（`DOCUMENT_REVIEW_REQUIRED`，可选 `reasonCodes`）；不要当成功 |

### 4.3 EAM 回调接口响应约定

以 EAM 测试接口文档为准：

| EAM HTTP 状态 | 含义 | Gateway 行为 |
|---|---|---|
| **200** | 已受理（含 `duplicate=true` 重放） | 停止重试 |
| **401** | 验签失败 / 时间戳非法 | **不重试**（死信） |
| **403** | EAM 未配置 outbound secret | **不重试**（死信） |
| `408` / `429` / **5xx** | 超时、限流或 EAM 内部异常 | 可重试 |
| 其它 `4xx` | 永久失败 | 死信，不再重试 |

成功体示例：`{"code":0,"duplicate":false,"message":"retrievable"}`。同一 `deliveryId` 重放返回 200 且 `duplicate=true`。

重试策略（Gateway 侧）：最多 **8** 次，退避 **1 / 5 / 30 / 120 / 600** 秒（之后保持 600）。  
回调失败 **不会** 把文档从 `ready` 打回失败。

EAM 实现要求：

1. 对相同 `deliveryId` **幂等**（重复推送仍返回 200）
2. 快速返回 200（重业务异步处理）
3. 校验 outbound 签名（`sha256=`，不是登记 inbound 的 `v1=`）；签名失败返回 **401**，secret 未配置返回 **403**，勿误用 5xx
4. 不要依赖中间态进度推送（本版本不推 parsing/indexing）
5. 只有 `status=retrievable` 才开放问答；`failed` / `review_required` 不得当成功

---

## 5. 错误码与可能原因

### 5.1 登记接口同步错误（HTTP 响应）

登记失败时 body 形如：

```json
{
  "code": "AUTH_SIGNATURE_INVALID",
  "message": "...",
  "requestId": "...",
  "retryable": false
}
```

| HTTP | code | 常见原因 | 建议处理 |
|---:|---|---|---|
| 401 | `AUTH_SIGNATURE_MISSING` | 缺少 Timestamp/Key-Id/Signature | 补齐三头 |
| 401 | `AUTH_SIGNATURE_INVALID` | secret 错、body 与签名不一致、path/query/method 不一致 | 用最终 raw body 重签 |
| 401 | `AUTH_TIMESTAMP_INVALID` | 时间戳非 10 位或时钟偏差过大 | 校时；换新 timestamp |
| 401 | `AUTH_REPLAY_DETECTED` | 完整旧签名被重放 | 重试必须换新签名 |
| 403 | `AUTH_BINDING_DENIED` | tenantId/sourceSystem 与 key 绑定不符 | 核对交付绑定 |
| 403 | `AUTH_BINDING_CONFLICT` | 顶层与 metadata 租户/来源冲突 | 对齐身份字段 |
| 403 | `ACL_DENIED` | credential 无权访问该 scope | 联系部署方授权 |
| 404 | `DOCUMENT_NOT_FOUND` | `reindex` 时文档版本不存在 | 先 upsert |
| 409 | `EVENT_ID_CONFLICT` | 同一 `eventId` 对应不同业务内容 | 换新 eventId 或恢复原 payload |
| 409 | `DOCUMENT_VERSION_CONFLICT` | 同版本但 sha256/路径/类型/size 等不一致 | 内容变则换 `sourceVersionId` 和新路径 |
| 422 | `VALIDATION_ERROR` | 顶层字段/文件名/JSON 结构非法 | 按契约修正 |
| 422 | `DOCUMENT_METADATA_INVALID` | metadata 缺字段、类型错、身份不一致、未知字段 | 按 metadata schema 修正 |
| 503 | `RAGFLOW_UNAVAILABLE` 等 | 依赖暂不可用（少见于登记瞬间） | 退避重试 |

收到 **4xx（除可约定重试外）**：登记未成功，**不会**有成功终态回调。  
收到 **202**：已受理，等待终态回调。

### 5.2 终态回调中的业务失败（异步）

这些通常出现在回调 `status=failed`（或诊断 GET 的 `errorCode`），而不是登记当时的 HTTP 错误：

| code | 常见原因 | 建议处理 |
|---|---|---|
| `DOCUMENT_SOURCE_NOT_FOUND` | 共享盘无文件、未挂载、relativePath 错、size 不匹配 | 确认 30 能读到 31 文件后再重投（新 event 或确认路径） |
| `DOCUMENT_HASH_MISMATCH` | 盘上 PDF 与提交 `sha256` 不一致 | 重算哈希；勿中途改文件 |
| `DOCUMENT_PARSE_FAILED` | RAGFlow 解析失败 | 查 PDF 是否损坏/加密/异常格式；必要时换版本重投 |
| `DOCUMENT_QUALITY_FAILED` / 质量失败 | 质量门未通过 | 按复核流程或优化文档后重投新版本 |
| `DOCUMENT_SYNC_FAILED` | 同步链路其它终态失败 | 结合 message；联系知识库侧排查 |
| `PARSER_APPLICATION_MISMATCH` | 解析配置读回不一致 | 联系知识库侧 |
| `RAGFLOW_UNAVAILABLE` | 解析服务暂不可用 | 可稍后以新事件重试（视业务约定） |

`status=review_required`：不是传输错误，而是**质量需人工复核**；此时 `retrievable=false`，且回调带 `error.code=DOCUMENT_REVIEW_REQUIRED`（可选 `reasonCodes`），EAM 不应当作入库成功。

### 5.3 回调通道本身的问题（EAM 侧）

| 现象 | 可能原因 |
|---|---|
| 一直收不到回调 | 回调 URL 未配成 `http://192.168.30.31:5105/api/v1/ai/feed/callback` / Gateway 未启用 `ENTERPRISE_CALLBACK_ENABLED` / 30→31:5105 不通 |
| Gateway 反复重试 | EAM 返回 5xx/超时/429 |
| Gateway 停止重试（死信） | EAM 返回 **401**/**403** 或其它 4xx，或重试耗尽 |
| 验签失败（EAM 401） | 用了 inbound `v1=` secret，或未按 `{timestamp}.{canonical_json}` 规则验签，或 body 被改写后再验 |

---

## 6. EAM 改造清单

### 必须做

- [ ] 适配登记 `202` 瘦身回执（不再读 `statusUrl`）
- [ ] **新增**终态回调接收 HTTP 接口（§4）
- [ ] 使用 outbound secret 验签（`sha256=`，不是登记的 `v1=`）
- [ ] 按 `deliveryId` 幂等处理
- [ ] 按 `status` 三态更新业务：`retrievable` / `failed` / `review_required`
- [ ] 向部署方提供回调 URL，完成 Gateway 侧配置联调

### 建议做

- [ ] 回调内快速 2xx + 异步落库
- [ ] 记录 `originatingEventId` / `externalDocumentId` / `sourceVersionId` 便于对账
- [ ] 保留登记失败时的 `requestId` 便于排障

### 不要做

- [ ] 不要在登记请求里传 `callbackUrl`
- [ ] 不要再把轮询 `statusUrl` 当正式主路径
- [ ] 不要直接调 RAGFlow / 内部 ticket
- [ ] 不要在回调验签里复用登记 inbound secret（除非双方明确约定同一值，默认分离）

---

## 7. 最小联调验收

1. PDF 写入共享目录并原子改名，Gateway 可读  
2. HMAC 登记返回 `202`，body 仅含受理字段且无 `statusUrl`  
3. 故意错误 secret → `AUTH_SIGNATURE_INVALID`  
4. 错误 tenant/source → `AUTH_BINDING_DENIED`  
5. 相同 `eventId` + 相同 payload 重试 → `202` 且 `deduplicated=true`（或等价幂等）  
6. 正常文件处理完成后，EAM 收到 `status=retrievable` 且验签通过  
7. 模拟缺文件/错哈希后，EAM 收到 `status=failed` 及错误 code  
8. 质量复核场景收到 `status=review_required`，业务不标成功  

---

## 8. 相关文档

| 文档 | 用途 |
|---|---|
| 本文 | EAM 投喂变更与对接总览 |
| `docs/integration/eam-feed-callback.md` | EAM 测试环境终态回调接口（31:5105） |
| `docs/integration/eam-feed-call-notice.md` | 联调参数与登记细节 |
| `docs/integration/device-system-hmac-handoff.md` | inbound HMAC 细则 |
| `docs/integration/eam-device-integration-guide.md` | 综合对接（含问答阶段） |
| `contracts/file-share-v3.yaml` | 登记/诊断 OpenAPI |
| `contracts/file-share-callback-v1.yaml` | 终态回调 OpenAPI |
