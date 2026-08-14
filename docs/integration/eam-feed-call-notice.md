# EAM 企业知识库投喂联调注意事项

版本：`1.1`

适用接口：`FILE_SHARE API v3.1.0` + `FILE_SHARE callback v1.0.0`

本文供 EAM 开发、测试和运维人员使用。EAM 只调用 Enterprise Gateway，不直接访问 RAGFlow、数据库、Redis、对象存储管理端口或 Gateway 内部接口。

给 EAM 的变更总览、错误码与对方需新增回调接口说明见：[`eam-file-feed-handoff-3.1.md`](./eam-file-feed-handoff-3.1.md)。

## 1. 联调参数

以下是当前测试环境示例，正式环境以部署方通过安全渠道交付的参数为准：

| 参数 | 当前测试值 | 说明 |
|---|---|---|
| Gateway 地址 | `http://192.168.30.30:5188` | 只填写 Gateway 根地址 |
| tenantId | `wp04e2e` | 顶层 body、metadata 和 HMAC binding 必须一致 |
| sourceSystem | `EAM` | 固定传 `EAM` |
| keyId | `device-local-key` | 放入 `X-TY-Key-Id` 请求头 |
| storageRootId | `device-share` | 文件共享逻辑根目录标识 |
| HMAC secret（登记 inbound） | 由部署方单独交付 | `v1=` 五行签名；不写入本文、前端、URL、日志或 Git |
| 终态回调 URL | `http://192.168.30.31:5105/api/v1/ai/feed/callback` | Gateway → EAM；运维写入 `ENTERPRISE_CALLBACK_ENDPOINTS` |
| 回调验签 | `sha256=` HMAC | **不是** 登记 inbound 的 `v1=`；outbound secret 与 inbound 分离 |

如果 EAM 与 Gateway 不在同一台机器上，不要使用 `127.0.0.1`。生产环境应使用 HTTPS 地址。

终态回调接口详见 [`eam-feed-callback.md`](./eam-feed-callback.md)。登记 JSON **禁止** `callbackUrl`。

## 2. 文件投喂方式

Gateway 不接收 PDF 二进制。EAM 必须先将 PDF 写入双方约定的文件共享目录，再调用登记接口。

本次联调的文件源目录是 31 服务器上的 `/data/zk/EAM/attachments`。该目录必须先以只读方式挂载到 30
服务器的 Gateway 宿主机目录，再由 Compose 绑定到容器内的 `/var/lib/tyrag/file-share`。因此请求中的
`relativePath` 只相对于 `/data/zk/EAM/attachments`，不包含这个绝对路径前缀。

推荐目录格式：

```text
eam/<tenantId>/<equipmentId>/<externalDocumentId>/<sourceVersionId>/<fileName>
```

推荐写入流程：

1. 先写入临时文件，例如 `Receipt-2303-9536.pdf.uploading`。
2. 写入完成后关闭文件，计算实际文件大小和 SHA-256。
3. 原子重命名为最终 `.pdf` 文件。
4. 确认 30 服务器的 Gateway 容器能够读取该文件。
5. 调用 `POST /enterprise/api/v3/documents`。

`relativePath` 只能是相对于 `storageRootId` 的相对路径，不能传 Windows 绝对路径、Linux 绝对路径、UNC 路径、`.` 或 `..`。

文件登记后不要覆盖或移动原文件。文件内容变化时必须使用新的 `sourceVersionId` 和新的文件路径。

## 3. HMAC 鉴权

所有 v3 文件登记和状态请求都必须携带：

```http
X-TY-Timestamp: <10位 Unix 秒时间戳>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<小写十六进制 HMAC-SHA256>
```

签名原文固定为五行，最后一行后不能追加换行：

```text
v1
<timestamp>
<大写 HTTP method>
<规范化 path 和排序后的 RFC3986 query>
<最终发送的原始 JSON body 的 SHA-256>
```

签名计算：

```text
signature = HMAC-SHA256(hmacSecret, UTF-8(canonicalText))
X-TY-Signature = v1=<signature 的小写十六进制值>
```

注意：请求 body 的 SHA-256、PDF 文件的 `sha256` 和 `source.etag` 是三个不同概念。修改 JSON 的任何空格、换行、字段顺序或字段值后，都必须对最终发送的原始 body 重新签名。

每次重试都必须生成新的 timestamp 和 signature。相同业务事件可以保留相同的 `eventId`，但不能重复使用完全相同的签名。

v3 文件接口不使用 `Authorization: Bearer`，也不使用 RAGFlow API key。

## 4. 登记请求

请求：

```http
POST {BASE_URL}/enterprise/api/v3/documents
Content-Type: application/json
Accept: application/json
X-TY-Timestamp: <timestamp>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<signature>
```

请求示例：

```json
{
  "eventId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "eventType": "upsert",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "sha256": "<PDF实际内容的64位SHA-256>",
  "fileName": "Receipt-2303-9536.pdf",
  "mediaType": "application/pdf",
  "source": {
    "kind": "FILE_SHARE",
    "storageRootId": "device-share",
    "relativePath": "eam/wp04e2e/GI01240033/FAC-8255-ATT-22/v1-1d9dd084c59e/Receipt-2303-9536.pdf",
    "size": 339464
  },
  "metadata": {
    "schema_version": 1,
    "tenant_id": "wp04e2e",
    "external_document_id": "FAC-8255-ATT-22",
    "source_system": "EAM",
    "equipment_id": "GI01240033",
    "fixed_asset_no": "GI01240033",
    "document_type": "UNPACK_ACCEPT",
    "document_version": "v1-1d9dd084c59e",
    "department_id": "2",
    "security_level": 2,
    "allow_group_ids": ["maintenance"],
    "deny_group_ids": [],
    "business_status": "active"
  }
}
```

### 4.1 顶层字段要求

| 字段 | 要求 |
|---|---|
| `eventId` | 必填，最长 128；同一业务事件重试时保持不变 |
| `eventType` | 只能是 `upsert` 或 `reindex` |
| `tenantId` | 非空，最长 64 |
| `sourceSystem` | 固定为 `EAM`，最长 64 |
| `externalDocumentId` | EAM 文档永久 ID，最长 128 |
| `sourceVersionId` | 文档版本，最长 64 |
| `sha256` | PDF 实际原始字节的 64 位十六进制 SHA-256 |
| `fileName` | 只能是文件名，不能包含 `/` 或 `\` |
| `mediaType` | 固定为 `application/pdf` |

### 4.2 metadata 字段要求

`metadata` 中以下身份字段必须与顶层 body 完全一致：

```text
metadata.tenant_id            = tenantId
metadata.external_document_id = externalDocumentId
metadata.source_system        = sourceSystem
metadata.document_version     = sourceVersionId
```

`document_type` 由 EAM 自己维护，服务端不再限制固定枚举。只要求非空字符串且最长 64 个字符，因此 `UNPACK_ACCEPT` 可以直接传入，并会作为属性保存。

`document_subtype`、`source_document_type` 是可选扩展属性，最长 128 个字符。普通 EAM 请求不需要同时传这两个字段。

`page_count`、`fixed_asset_no`、`asset_id`、`equipment_name` 等字段是可选的，缺少 `page_count` 不会导致 metadata 校验失败。

`security_level` 必须是 `0` 到 `9` 的整数；`business_status` 必须是 `active`、`superseded`、`disabled`、`deleted` 或 `review_required` 之一。

不要在 metadata 中增加服务端契约没有定义的字段。未定义字段会导致 `DOCUMENT_METADATA_INVALID`。

### 4.3 source 字段要求

`source.kind` 必须是 `FILE_SHARE`。`storageRootId` 必须使用部署方交付的逻辑名称，当前测试值为 `device-share`。

`source.size` 如果提供，必须是 PDF 的实际字节数。

EAM 当前请求**不要传 `source.etag`**。该字段是通用 FILE_SHARE 契约保留的可选文件版本标识，
不是 PDF 的 SHA-256；只有双方另行约定独立版本标识时才传。顶层 `sha256` 必须保留，不能
重复填入 `source.etag`。

## 5. 响应和终态回调（3.1.0）

首次登记成功返回 `202`，只表示 Gateway 已受理任务，不表示文档已经完成解析和索引。登记响应是瘦身受理回执，**不再返回 `statusUrl`**：

```json
{
  "operationId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "deduplicated": false,
  "updatedAt": "2026-08-13T02:00:00+00:00"
}
```

正式对接路径：测试环境回调地址固定为 `POST http://192.168.30.31:5105/api/v1/ai/feed/callback`，由运维写入 Gateway 的 `ENTERPRISE_CALLBACK_ENDPOINTS`（字符串 URL，按 `sourceSystem` 映射）。登记请求体**禁止**携带 `callbackUrl`。文档到达终态后，Gateway 主动 POST 签名回调，`status` 仅为：

- `retrievable`：可检索（质量通过且已提升为 current version），才开放问答
- `failed`：处理失败
- `review_required`：质量门要求复核（不会自动可检索）

回调验签是 `sha256=`（`{timestamp}.{canonical_json}`），**不是** 登记 inbound 的 `v1=`。EAM 响应语义：

- **200**：已受理（含 `duplicate=true`）→ Gateway 停止重试
- **401**：验签失败 / 时间戳非法 → 死信，不重试
- **403**：EAM 未配 outbound secret → 死信，不重试
- **5xx**：可重试；最多 8 次，退避 1/5/30/120/600 秒

接口说明见 [`eam-feed-callback.md`](./eam-feed-callback.md)，契约见 `contracts/file-share-callback-v1.yaml`。投递失败 **不会** 回滚已成功的入库状态。

GET `/enterprise/api/v3/documents/{id}/status` 仍保留给 Console/运维诊断，不是 EAM 正式主路径。

## 6. 常见错误

| HTTP / 状态 | code | 原因和处理 |
|---|---|---|
| `401` | `AUTH_SIGNATURE_MISSING` | HMAC 三个请求头不完整 |
| `401` | `AUTH_SIGNATURE_INVALID` | keyId、secret、body、path、query 或 method 不一致 |
| `401` | `AUTH_TIMESTAMP_INVALID` | 时间戳格式错误或调用服务器时间未同步 |
| `401` | `AUTH_REPLAY_DETECTED` | 重试复用了完整旧签名 |
| `403` | `AUTH_BINDING_DENIED` | tenantId/sourceSystem 与交付的 key 绑定不一致 |
| `403` | `AUTH_BINDING_CONFLICT` | 顶层 body 和 metadata 中出现互相冲突的租户或来源 |
| `409` | `EVENT_ID_CONFLICT` | 相同 eventId 对应了不同业务内容 |
| `409` | `DOCUMENT_VERSION_CONFLICT` | 同一文档版本的 SHA、路径、类型、size 或 etag 不一致 |
| `422` | `VALIDATION_ERROR` | 顶层字段、source 字段、文件名或 JSON 结构不符合要求 |
| `422` | `DOCUMENT_METADATA_INVALID` | metadata 缺字段、字段类型错误、身份不一致或包含未知字段 |
| 状态 `failed` | `DOCUMENT_SOURCE_NOT_FOUND` | 文件不存在、共享目录未挂载、路径错误、size/etag 不匹配 |
| 状态 `failed` | `DOCUMENT_HASH_MISMATCH` | PDF 实际内容与提交的 SHA-256 不一致 |
| 状态 `failed` | `DOCUMENT_PARSE_FAILED` | RAGFlow 解析任务失败 |
| 状态 `failed` | `RAGFLOW_UNAVAILABLE` | RAGFlow 不可用或文档读回失败 |

收到 `422` 时，登记没有成功落库，无需等待回调。收到 `202` 后，以终态回调为准；运维诊断可用 GET status 读取 `status`、`errorCode` 和 `error`。

## 7. 重试规则

1. `422`：修正请求参数后重新发送；确认最终 body 后重新签名。
2. `401`：修正 HMAC、时间戳或 key 配置，不要无条件重试。
3. `403`：检查 tenantId、sourceSystem 和 KeyId 的服务端绑定。
4. `409 EVENT_ID_CONFLICT`：不要复用该 eventId 发送不同内容；内容变更时生成新的 eventId。
5. 同一业务内容重试：保留原 `eventId`，每次生成新的 timestamp 和 signature。
6. `202` 后等待 Gateway 终态回调；回调接收接口应对相同 `deliveryId` 幂等。
7. PDF 内容变化：新建 `sourceVersionId`、新路径、新 SHA-256 和新的业务事件。

## 8. 安全要求

- HMAC secret 只能保存在 EAM 后端或 Secret Manager。
- 不要把 HMAC secret 放入浏览器、前端配置、URL、请求 body、日志、Postman Collection 或 Git。
- 日志只记录 `eventId`、文档 ID、HTTP 状态、错误 code 和 requestId，不记录 secret、完整 PDF 内容或完整签名。
- EAM 只访问 Gateway，不直接访问 RAGFlow、MySQL、Redis、MinIO 或 Gateway 数据库。

## 9. 联调验收清单

- [ ] PDF 已写入双方约定的共享目录。
- [ ] PDF 已完成原子改名，最终路径可以被 30 服务器 Gateway 读取。
- [ ] `sha256` 是实际 PDF 内容的 SHA-256。
- [ ] `document_type` 使用 EAM 实际配置名称，例如 `UNPACK_ACCEPT`。
- [ ] 顶层身份字段与 metadata 身份字段完全一致。
- [ ] EAM 请求未传 `source.etag`；顶层 `sha256` 未重复填入其他字段。
- [ ] 正常登记返回 `202` 瘦身受理回执（含 `operationId`，无 `statusUrl`）。
- [ ] EAM 回调 URL 已配置到 Gateway，并能接收 `retrievable` / `failed` / `review_required`。
- [ ] 只有收到 `status=retrievable`（或诊断侧确认 `retrievable=true` 且 `qualityStatus=passed`）时才开放问答。
- [ ] 错误 secret 能收到 `AUTH_SIGNATURE_INVALID`。
- [ ] 错误 tenant/source 能收到 `AUTH_BINDING_DENIED`。
- [ ] 相同 eventId 和相同内容重试不会生成重复文档。

接口详细契约：

- `docs/integration/eam-feed-callback.md`（测试环境终态回调）
- `contracts/file-share-v3.yaml`（3.1.0）
- `contracts/file-share-callback-v1.yaml`
- `contracts/metadata-schema.json`
