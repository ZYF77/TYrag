# EAM 通用 JSON 投喂变更说明（Document Feed 3.2.0）

> 状态：Gateway 代码与 3.2 OpenAPI 已实现；本机真实 HTTP 联调验收尚未通过，未部署
> 日期：2026-08-28
> 面向：EAM 开发 / 测试 / 运维
> 兼容基线：FILE_SHARE API 3.1.0、终态回调 1.0.0

本文只说明本次新增的 `INLINE_JSON` 投喂能力。原 FILE_SHARE、HMAC、状态查询、生命周期和终态回调规则保持不变。

## 1. 本次变更

EAM 继续使用同一个入口：

```http
POST /enterprise/api/v3/documents
```

不新增 `/facility`、`/repair`、`/maintenance` 等业务接口。新增：

```json
{
  "source": {
    "kind": "INLINE_JSON",
    "content": {}
  }
}
```

`source.content` 内部不固定字段。EAM 后续新增普通 JSON 字段时，不需要 Gateway 新增接口或修改 DTO。

每份 JSON 必须通过 `metadata.equipment_id` 关联一个设备。

## 2. 与 FILE_SHARE 的关系

| 项目 | FILE_SHARE 3.1 | INLINE_JSON 3.2 |
|---|---|---|
| URL | `/enterprise/api/v3/documents` | 相同 |
| 认证 | 现有 HMAC | 相同 |
| `source.kind` | `FILE_SHARE` | `INLINE_JSON` |
| 内容来源 | 共享目录中的 PDF | 请求内 `source.content` |
| `mediaType` | `application/pdf` | `application/json` |
| `fileName` | PDF 文件名 | 必须以 `.json` 结尾 |
| 内容 `sha256` | EAM 必填，校验文件字节 | EAM 不发送，由 Gateway 内部计算 |
| 设备关联 | `metadata.equipment_id` | 相同且必填 |
| 完成通知 | `document.terminal` | 相同 |

## 3. 登记请求

### 3.1 请求头

```http
POST {BASE_URL}/enterprise/api/v3/documents
Content-Type: application/json
Accept: application/json
X-TY-Timestamp: <10 位 Unix 秒>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<小写 hex HMAC-SHA256>
```

HMAC 是现有 v3.1 系统间认证，不是 JSON 新增机制。签名组件仍会在内部计算最终 raw body 的 SHA-256，用于把签名和请求正文绑定；该值不是 JSON 请求字段，EAM 不需要额外上传。

签名算法和密钥交付方式继续参见 [`device-system-hmac-handoff.md`](./device-system-hmac-handoff.md)。不要发送 Bearer Token、RAGFlow API Key 或 `callbackUrl`。

### 3.2 完整请求示例

```json
{
  "eventId": "FAC-10086-MASTER-v1-submit-001",
  "eventType": "upsert",
  "tenantId": "tenant-a",
  "sourceSystem": "EAM",
  "externalDocumentId": "FAC-10086-MASTER",
  "sourceVersionId": "v1",
  "fileName": "FAC-10086-MASTER.json",
  "mediaType": "application/json",
  "source": {
    "kind": "INLINE_JSON",
    "content": {
      "equipment_name": "生产一线离心机",
      "identity": {
        "equipment_id": "EQ-CF-001",
        "facility_code": "CF-01-001"
      },
      "technical_profile": {
        "manufacturer": "示例设备制造有限公司",
        "model": "CF-1200",
        "voltage_v": 380,
        "motor_power_kw": 15
      },
      "anything_added_later": {
        "value": 123
      }
    }
  },
  "metadata": {
    "schema_version": 1,
    "tenant_id": "tenant-a",
    "external_document_id": "FAC-10086-MASTER",
    "source_system": "EAM",
    "equipment_id": "EQ-CF-001",
    "document_type": "EQUIPMENT_LEDGER",
    "document_version": "v1",
    "department_id": "12",
    "security_level": 2,
    "business_status": "active"
  }
}
```

注意：INLINE_JSON 请求中没有业务内容 `sha256` 字段。

### 3.3 顶层字段

| 字段 | 必填 | 规则 |
|---|---:|---|
| `eventId` | 是 | 一次业务投递事件 ID；同一网络重试保持不变 |
| `eventType` | 是 | `upsert` 或既有 `reindex` |
| `tenantId` | 是 | 必须与 HMAC credential binding 和 Metadata 一致 |
| `sourceSystem` | 是 | 当前固定为 `EAM` |
| `externalDocumentId` | 是 | 逻辑知识对象稳定 ID |
| `sourceVersionId` | 是 | 内容变化时使用新版本 ID |
| `fileName` | 是 | 必须是不含路径的 `.json` 文件名 |
| `mediaType` | 是 | 固定 `application/json` |
| `source` | 是 | `kind=INLINE_JSON`，`content` 为 JSON object |
| `metadata` | 是 | 沿用现有严格 Metadata；`equipment_id` 必填 |
| `batchId` | 否 | 既有批次标识 |
| `sha256` | 不发送 | 由 Gateway 内部计算 |
| `callbackUrl` | 禁止 | 回调地址只来自 Gateway 服务端配置 |

既有 Metadata 身份字段仍必须与顶层一致：

```text
metadata.tenant_id            = tenantId
metadata.external_document_id = externalDocumentId
metadata.source_system        = sourceSystem
metadata.document_version     = sourceVersionId
```

## 4. source.content 规则

`source.content` 必须是 JSON object。内部允许：

- string、number、boolean、null；
- 任意业务 object；
- 标准 JSON array；
- EAM 后续新增、修改或删除的普通业务字段。

普通未知字段默认进入 RAGFlow 的文本和向量索引，但不会自动成为 Metadata 精确过滤字段。例如：

```json
{
  "technical_profile": {
    "motor_power_kw": 15
  }
}
```

`motor_power_kw` 会成为可检索内容，但不会自动生成：

```json
{
  "meta_fields": {
    "motor_power_kw": 15
  }
}
```

### 4.1 安全限制

以下内容不得投喂：

- password、passwd、pwd、token、secret、apiKey、authorization、cookie、privateKey、clientSecret 等凭据字段；
- base64、fileContent、binary、attachmentContent 等明显承载文件或二进制内容的字段；
- 请求总大小超过 2 MiB 的 JSON；
- `source.content` 嵌套超过 20 层的 JSON。

字段名匹配忽略大小写以及 `_`、`-` 差异。命中后 Gateway 返回同步 422，不会静默删除字段后继续登记。

本接口不使用通用 Base64 内容猜测器；EAM 应通过 FILE_SHARE 或其他文件接口传文件，不要把文件字节塞入 JSON 字符串。

### 4.2 equipment_id

- `metadata.equipment_id` 必填，是设备 Scope、ACL 和精确过滤的权威值；
- `source.content` 可以不重复设备号；
- 如果 `source.content` 中明确出现 `equipment_id`，其值必须与 Metadata 一致，否则返回 422；
- 第一阶段一份 JSON 只归属一个设备。多个设备应分别形成多个逻辑知识对象。

## 5. Gateway 与 RAGFlow 如何处理

EAM 不需要实现或配置以下处理：

1. Gateway 校验固定 Envelope、设备关联、安全限制和幂等；
2. Gateway 稳定序列化 `source.content`，并计算内部内容哈希；
3. Gateway 将内容作为 `.json` 文档调用现有 RAGFlow 上传接口；
4. RAGFlow 内置 JSON Parser 按对象和数组结构分块；
5. RAGFlow 完成 Embedding 和索引；
6. Gateway 读回验证解析已完成且至少存在一个非空 Chunk；
7. 版本满足 Scope、状态和质量门后进入可检索状态。

首版不建设：

- 业务 Profile/YAML 字段映射；
- Gateway 自定义 JSON 分块器；
- Virtual Document + Add Chunk 链路；
- JSONPath 存储或字段级 citation；
- 数据库在线配置平台。

## 6. 受理、幂等和版本

成功登记仍返回 202：

```json
{
  "operationId": "FAC-10086-MASTER-v1-submit-001",
  "externalDocumentId": "FAC-10086-MASTER",
  "sourceVersionId": "v1",
  "deduplicated": false,
  "updatedAt": "2026-08-28T02:00:00+00:00"
}
```

HTTP 202 只表示登记已受理，不表示已经可以问答。

| 情况 | 结果 |
|---|---|
| 相同 `eventId` + 相同请求，且正在处理/已成功 | 202，返回原操作，`deduplicated=true` |
| 相同 `eventId` + 相同请求，且上次失败并标记 `retryable=true` | 202，开启新处理轮次，`deduplicated=false` |
| 相同 `eventId` + 不同请求 | 409 `EVENT_ID_CONFLICT` |
| 相同业务版本 + 相同内容 | 202，复用已有版本，不重复 Embedding |
| 相同业务版本 + 不同内容 | 409 `DOCUMENT_VERSION_CONFLICT` |
| 新 `sourceVersionId` | 创建新候选版本；成功后替换当前版本 |

EAM 不需要计算 Gateway 的内部内容哈希。EAM 只需要遵守：**内容变化时使用新的 `sourceVersionId`**。

## 7. 状态与终态回调

诊断状态查询保持不变：

```http
GET {BASE_URL}/enterprise/api/v3/documents/{externalDocumentId}/status
    ?tenantId=tenant-a
    &sourceSystem=EAM
    &sourceVersionId=v1
```

正式完成通知继续使用既有 `document.terminal` 回调。JSON 不新增回调 URL、签名算法或 Body 字段。

| status | EAM 动作 |
|---|---|
| `retrievable` | 标记投喂成功，可以进入问答 |
| `failed` | 标记失败，读取 `error` 并按 `retryable` 处理 |
| `review_required` | 不得当作成功 |

EAM 继续按 `deliveryId` 幂等处理回调。完整回调协议参见 [`eam-feed-callback.md`](./eam-feed-callback.md)。

如果 EAM 使用设备内容指纹作为稳定 `eventId`，失败后的重新投喂约定参见 [`eam-event-id-fingerprint-retry-notice-3.2.md`](./eam-event-id-fingerprint-retry-notice-3.2.md)。Gateway 已补全失败重处理和回调轮次支持，正式生效仍以本机 HTTP/EAM 联调通过为准。

## 8. INLINE_JSON 相关错误

| HTTP | code | 常见原因 | EAM 处理 |
|---:|---|---|---|
| 409 | `EVENT_ID_CONFLICT` | 同一 eventId 对应不同请求 | 恢复原请求或使用新 eventId |
| 409 | `DOCUMENT_VERSION_CONFLICT` | 同一版本内容不同 | 内容变化时换新 sourceVersionId |
| 422 | `VALIDATION_ERROR` | content 非 object、fileName 非 `.json`、超限或包含禁止字段 | 修正请求 |
| 422 | `DOCUMENT_METADATA_INVALID` | equipment_id 缺失/冲突或身份字段不一致 | 修正 Metadata |
| 503 | `RAGFLOW_UNAVAILABLE` | RAGFlow 或 Embedding 暂不可用 | 退避重试 |

HMAC、credential binding、ACL 和回调错误沿用现有 v3.1/v1.0 协议，不在本文重复定义。

## 9. EAM 改造清单

EAM 需要：

1. 继续调用现有文档登记 URL；
2. 设置 `source.kind=INLINE_JSON`、`mediaType=application/json`；
3. 将任意业务 JSON object 放入 `source.content`；
4. 提供稳定的 `externalDocumentId`、`sourceVersionId` 和必填 `metadata.equipment_id`；
5. `fileName` 使用 `.json` 后缀；
6. 不发送业务内容 `sha256`；
7. 复用现有 HMAC 签名组件和终态回调处理；
8. 内容变化时生成新版本；网络重试保持 eventId 不变并重新生成现有 HMAC timestamp/signature。

EAM 不需要：

- 为不同 JSON 类型调用不同接口；
- 预先登记 `source.content` 的所有字段；
- 提供 Parser、Profile、Chunk 或 Embedding 配置；
- 把未知字段声明成 Metadata；
- 计算 canonical JSON 内容哈希；
- 直接调用 RAGFlow、数据库、Redis 或对象存储管理接口。

## 10. 最小联调验收

1. INLINE_JSON 首次登记返回 202，最终回调为 `retrievable`；
2. 新增未知普通字段后仍可登记，该字段可被关键词或语义召回，但不成为 Metadata filter；
3. 处理中/成功的相同事件不重复生成向量；可重试失败的相同事件开启新轮次，同版本修改内容返回 409；
4. 缺少/冲突 equipment_id、敏感字段、超限和跨设备 Scope 失败关闭；
5. 原 FILE_SHARE 3.1 登记、解析、引用和回调不受影响。

## 11. 当前状态与相关文档

- Gateway 代码与 Document Feed 3.2 OpenAPI 已在当前工作区实现；
- 稳定 `eventId` 的可重试失败重处理、处理轮次质量评估和回调轮次幂等已在 Gateway 代码中实现；
- 已验证的聚焦 INLINE_JSON、FILE_SHARE HMAC 和 E2E runner 离线测试已通过；本轮新增的已有文档重解析调用链测试待本机 PostgreSQL 恢复后执行；
- 本机真实 HTTP runner 因当前 shell 缺少所需联调配置而返回 `BLOCKED`，因此尚未形成 RAGFlow 实际解析、查询召回和终态回调的同轮验收结论；
- `contracts/file-share-v3.yaml` 3.1.0 和回调 1.0.0 保持不变；
- 当前变更未部署；完成本机真实 HTTP E2E 前不得对外宣称已上线；
- 不部署 30 联调机，除非另行明确授权。

相关基线：

- [`eam-file-feed-handoff-3.1.md`](./eam-file-feed-handoff-3.1.md)
- [`device-system-hmac-handoff.md`](./device-system-hmac-handoff.md)
- [`eam-feed-callback.md`](./eam-feed-callback.md)
- [`eam-event-id-fingerprint-retry-notice-3.2.md`](./eam-event-id-fingerprint-retry-notice-3.2.md)
- [`../../contracts/document-feed-v3.2.yaml`](../../contracts/document-feed-v3.2.yaml)
- [`../../contracts/file-share-v3.yaml`](../../contracts/file-share-v3.yaml)
- [`../../contracts/file-share-callback-v1.yaml`](../../contracts/file-share-callback-v1.yaml)
