# 设备管理系统对接凭证与接口交接单

适用接口：`FILE_SHARE API v3.0.0`

本文用于交付给设备管理系统开发方。本文不包含 HMAC secret；secret 必须由部署方通过单独的安全渠道交付。

## 1. 本次联调参数

| 参数 | 测试环境值 | 说明 |
|---|---|---|
| Gateway 地址 | `http://192.168.30.30:5188` | 设备管理系统只访问 Gateway |
| `keyId` | `device-local-key` | 放在 `X-TY-Key-Id` 请求头 |
| `tenantId` | `wp04e2e` | 请求 body 和 metadata 必须使用此值 |
| `sourceSystem` | `EAM` | 请求 body 和 metadata 必须使用此值 |
| `storageRootId` | `device-share` | 双方约定的只读文件共享根目录 |
| HMAC secret | 由部署方单独提供 | 不写入本文、代码、请求 body 或 Git |

正式环境参数以部署方最终交付为准。`credentialId` 是 Gateway 内部配置名称，不需要发送给接口。

## 2. Secret 获取方式

设备管理系统不能通过 Gateway API 获取 HMAC secret。

交付流程如下：

1. 部署方在 Gateway 的 `ENTERPRISE_SYNC_HMAC_CREDENTIALS` 中配置 credential。
2. 部署方将同一个 `keyId` 和 secret 配置到设备管理系统的 Secret Manager。
3. 部署方通过密码管理器、Secret Manager 授权或一次性安全链接将 secret 交给对方负责人。
4. 设备管理系统仅在服务端进程内读取 secret，不把 secret 放到浏览器、前端代码、日志、URL、Postman Collection 或普通配置导出文件中。

HMAC secret 是双方共享的长期凭证。没有该 secret，设备管理系统无法通过认证。每个生产调用系统应使用独立的 `keyId` 和 secret。

## 3. 文件接口认证

以下 v3 接口使用 HMAC：

```text
POST /enterprise/api/v3/documents
GET  /enterprise/api/v3/documents/sync-status
GET  /enterprise/api/v3/documents/{externalDocumentId}/status
```

请求头：

```http
X-TY-Timestamp: <10位Unix秒时间戳>
X-TY-Key-Id: device-local-key
X-TY-Signature: v1=<小写十六进制签名>
```

v3 文件接口不使用 `Authorization: Bearer`，也不使用 RAGFlow API key。

## 4. HMAC 签名算法

签名输入由以下五行组成，最后一行后不追加换行：

```text
v1
<timestamp>
<大写HTTP method>
<规范化path及排序后的RFC3986 query>
<原始请求体bytes的SHA-256小写十六进制值>
```

计算方式：

```text
signature = HMAC-SHA256(secret, canonical_input)
X-TY-Signature = "v1=" + lowercase_hex(signature)
```

签名要求：

- `timestamp` 必须是 10 位 Unix 秒时间戳，服务器允许时间偏差为 ±300 秒。
- 请求体必须先序列化为最终发送的 UTF-8 bytes，再计算 SHA-256；签名后不能修改 body 的空格、换行或字段顺序。
- query 参数按 RFC3986 编码，并按编码后的 key/value 排序。
- GET 状态查询的 body 为空，空 body 的 SHA-256 也必须参与签名。
- 每次重试都必须生成新的 timestamp 和 signature，但可以保持相同的 `eventId` 以实现业务幂等。
- 同一个完整签名请求不能重复发送，服务器会进行 10 分钟防重放校验。

## 5. 文件提交方式

设备管理系统先将不可变 PDF 放入双方约定的只读文件共享，然后提交文件坐标、SHA-256 和业务元数据。PDF 二进制不通过 Gateway 上传。

```http
POST /enterprise/api/v3/documents
Content-Type: application/json
Accept: application/json
X-TY-Timestamp: <timestamp>
X-TY-Key-Id: device-local-key
X-TY-Signature: v1=<signature>
```

请求体示例：

EAM 请求当前不传 `source.etag`。顶层 `sha256` 是 PDF 内容摘要；没有双方另行约定的
独立文件版本标识时，不要增加 `source.etag`，也不要将 `sha256` 重复填入其中。

```json
{
  "eventId": "evt-device-manual-001",
  "eventType": "upsert",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "externalDocumentId": "DEVICE-MANUAL-001",
  "sourceVersionId": "v1",
  "sha256": "<PDF文件64位SHA-256>",
  "fileName": "device-manual.pdf",
  "mediaType": "application/pdf",
  "source": {
    "kind": "FILE_SHARE",
    "storageRootId": "device-share",
    "relativePath": "manuals/device-manual.pdf",
    "size": 123456
  },
  "metadata": {
    "schema_version": 1,
    "tenant_id": "wp04e2e",
    "external_document_id": "DEVICE-MANUAL-001",
    "source_system": "EAM",
    "equipment_id": "EQ-001",
    "fixed_asset_no": "FA-001",
    "document_type": "PRODUCT_MANUAL",
    "document_version": "v1",
    "department_id": "maintenance",
    "security_level": 2,
    "allow_group_ids": ["maintenance"],
    "deny_group_ids": [],
    "business_status": "active",
    "page_count": 10
  }
}
```

约束：

- `mediaType` 必须为 `application/pdf`。
- `fileName` 不能包含 `/` 或 `\\`。
- `relativePath` 必须位于指定 `storageRootId` 根目录内。
- body、`metadata` 和 HMAC credential 的 `tenantId/sourceSystem` 必须一致。
- `sha256` 必须是实际 PDF 文件内容的 SHA-256，不能使用文件名或业务 ID 的摘要。
- PDF 登记后应保持不可变；文件内容变化时必须提交新的 `sourceVersionId`。
- `document_type` 由设备管理系统维护并直接传入；服务端只校验非空且不超过 64 个字符。

## 6. 响应与终态回调

首次接收成功返回 HTTP `202` 瘦身受理回执（FILE_SHARE 3.1.0），不代表解析和索引已经完成：

```json
{
  "operationId": "evt-device-manual-001",
  "externalDocumentId": "DEVICE-MANUAL-001",
  "sourceVersionId": "v1",
  "deduplicated": false,
  "updatedAt": "2026-08-13T02:00:00+00:00"
}
```

正式对接使用 Gateway → 设备系统的终态回调（见 `contracts/file-share-callback-v1.yaml`），登记响应不再包含 `statusUrl`。设备系统需提供回调接收 URL，并由运维配置到 Gateway。

GET status 仅作诊断：对 path+query 重新计算 HMAC，使用新的 timestamp/signature。只有回调 `status=retrievable`（或诊断侧确认 `retrievable=true` 且质量通过）时才开放问询。

## 7. 常见错误

| HTTP 状态 | 错误码 | 处理方式 |
|---:|---|---|
| 401 | `AUTH_SIGNATURE_MISSING` | 检查三个 HMAC 请求头是否完整 |
| 401 | `AUTH_SIGNATURE_INVALID` | 检查 secret、raw body、path、query、method 是否一致 |
| 401 | `AUTH_TIMESTAMP_INVALID` | 校准调用方服务器时间 |
| 401 | `AUTH_REPLAY_DETECTED` | 重试时生成新的 timestamp/signature |
| 403 | `AUTH_BINDING_DENIED` | 检查 `tenantId/sourceSystem` 是否为交付值 |
| 403 | `ACL_DENIED` | 检查调用 credential 的授权范围 |
| 409 | `EVENT_ID_CONFLICT` | 同一个 `eventId` 不得对应不同 payload |
| 422 | `DOCUMENT_METADATA_INVALID` | 按契约检查 metadata 和身份字段 |
| 503 | `ASSET_REGISTRY_UNAVAILABLE` | 联系部署方检查 Asset Registry |
| 503 | `RAGFLOW_UNAVAILABLE` | 联系部署方检查解析/索引服务 |

## 8. 双方职责

部署方负责：

- 提供可访问的 Gateway 地址。
- 配置并启用 HMAC credential。
- 安全交付 `keyId` 和 secret。
- 配置 `tenantId/sourceSystem` 绑定。
- 配置只读 FILE_SHARE 根目录、Asset Registry、RAGFlow 和 Redis/Valkey。

设备管理系统负责：

- 在服务端安全保存 secret。
- 实现 HMAC-SHA256 签名和时间戳处理。
- 将 PDF 放到约定的文件共享并计算真实 SHA-256。
- 提交符合 v3 契约的 JSON。
- 提供终态回调接收端点并校验 outbound 签名。
- 使用稳定的 `eventId` 和 `sourceVersionId` 实现重试及幂等。

## 9. 联调验收清单

- [ ] HMAC 正常注册返回 `202` 瘦身受理回执（无 `statusUrl`）。
- [ ] 回调 URL 已配置，并能接收 `retrievable` / `failed` / `review_required`。
- [ ] 错误 secret 返回 `AUTH_SIGNATURE_INVALID`。
- [ ] 重放相同签名返回 `AUTH_REPLAY_DETECTED`。
- [ ] 错误 tenant/source 返回 `AUTH_BINDING_DENIED`。
- [ ] 重复 `eventId` 和相同 payload 结果幂等。
- [ ] 文件终态达到 `retrievable`，或返回明确失败/复核通知。
- [ ] 设备管理系统不直接访问 RAGFlow、Redis、数据库、对象存储管理端口或任何 Gateway 内部接口。

## 10. 安全交付记录

以下内容由部署方和对方负责人通过安全渠道填写，不要在本文或普通聊天中填写 secret：

| 项目 | 内容 |
|---|---|
| 环境 | 测试 / 生产 |
| Gateway 地址 |  |
| `keyId` |  |
| `tenantId` |  |
| `sourceSystem` |  |
| Secret 交付渠道 |  |
| Secret 接收人 |  |
| Credential 生效时间 |  |
| Credential 失效/轮换时间 |  |

完整接口契约：`contracts/file-share-v3.yaml`。
