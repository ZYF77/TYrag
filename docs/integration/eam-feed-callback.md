# EAM 投喂终态回调说明

面向：知识库 Gateway / 联调  
契约：FILE_SHARE callback v1.0.0  
环境：31 测试（`http://192.168.30.31:5105`）

本文为 EAM 提供的测试环境正式回调接口说明，与仓库契约 `contracts/file-share-callback-v1.yaml` 对齐。

---

## 1. 流程

```text
EAM 写 PDF 到 FILE_SHARE
  → HMAC 登记 POST /enterprise/api/v3/documents
  → Gateway 立即 202（已受理，无 statusUrl）
  → Gateway 解析/索引/质量门完成后
  → Gateway POST 终态回调到 EAM
  → EAM 验签、按 deliveryId 幂等、按 status 更新投喂日志
```

- `202` 只表示已受理，不是可问答。
- 正式终态 **只认回调**，EAM 不再轮询 `statusUrl`。
- 登记 JSON **禁止** 带 `callbackUrl`；回调地址由双方运维写在 Gateway。

---

## 2. 回调地址

```http
POST http://192.168.30.31:5105/api/v1/ai/feed/callback
```

Gateway 配置示例：

```text
ENTERPRISE_CALLBACK_ENDPOINTS={"EAM":"http://192.168.30.31:5105/api/v1/ai/feed/callback"}
ENTERPRISE_CALLBACK_ENABLED=true
ENTERPRISE_CALLBACK_HMAC_SECRET=<outbound secret，与登记 inbound 分离>
```

密钥向 EAM 运维索取（系统设置 `aiFeedEnterpriseCallbackHmacSecret`），**不要**复用登记 inbound HMAC，**不要**写入 Git/文档。

---

## 3. 请求头

| 头 | 必填 | 说明 |
|---|---|---|
| `Content-Type` | 是 | `application/json` |
| `X-TY-Timestamp` | 是 | 10 位 Unix 秒（UTC） |
| `X-TY-Signature` | 是 | `sha256=<小写 hex>` |
| `X-TY-Key-Id` | 否 | EAM 当前不校验，可带 |

时间窗：±300 秒（EAM 设置 `aiFeedCallbackTimestampSkewSeconds`，下限 30 秒）。

---

## 4. 验签（与登记 inbound 不同）

登记是 `v1=` + 五行 canonical；**回调不是那套**。

```text
canonical_body = 对 JSON 对象做 key 字典序、紧凑无空格序列化
signed         = "{X-TY-Timestamp}." + canonical_body
expected       = "sha256=" + hex(HMAC-SHA256(outboundSecret, UTF-8(signed)))
```

要点：

- 必须对 **原始 body** 做 canonical，不要改空格/字段顺序后再签。
- 签名前缀是 `sha256=`，不是登记的 `v1=`。
- 验签失败 EAM 返回 **401**（永久类，Gateway 不要当 5xx 重试）。
- 未配置 outbound secret 时 EAM 返回 **403**。

---

## 5. 请求体

`status` **只允许三值**：`retrievable` / `failed` / `review_required`。

| 字段 | 必填 | 说明 |
|---|---|---|
| `deliveryId` | 是 | 本次投递唯一 ID；EAM 按此幂等 |
| `eventType` | 建议 | 如 `document.terminal` |
| `originatingEventId` | 是 | 对应登记 `eventId` |
| `externalDocumentId` | 是 | EAM 文档永久 ID |
| `sourceVersionId` | 是 | 文档版本 |
| `status` | 是 | 三态之一 |
| `timestamp` | 建议 | ISO-8601 |
| `payloadVersion` | 建议 | `"1"` |
| `tenantId` | 建议 | `wp04e2e` |
| `sourceSystem` | 建议 | `EAM` |
| `qualityStatus` | 建议 | `passed` / `review_required` / `failed` / `unknown` / `null` |
| `retrievable` | 建议 | 成功终态 `true`，否则 `false` |
| `error` | `failed` / `review_required` | `{code,message,retryable[,reasonCodes]}`；成功为 `null` |

EAM 登记侧 ID 规则（回调应对齐）：

```text
externalDocumentId = FAC-{设备Id}-ATT-{附件Id}
sourceVersionId    = v{附件版本}-{sha256前12位}
eventId            = {externalDocumentId}-{sourceVersionId}
```

例：`FAC-8255-ATT-22` + `v1-1d9dd084c59e` → `originatingEventId=FAC-8255-ATT-22-v1-1d9dd084c59e`

---

## 6. EAM 如何处理 `status`

| status | 业务含义 | EAM 动作 | 台账展示 |
|---|---|---|---|
| `retrievable` | 已解析且质量通过，可检索 | 主日志 Success | **可问答**，开放问询 |
| `failed` | 处理失败 | 主日志 Failed，写入 `error` | **失败** |
| `review_required` | 质量门要人工复核，不会自动变可检索 | 主日志 ReviewRequired，写入 `error` | **待复核**，不当成功 |

不要用 `retrievable` 布尔或 citation 数量反推 `status`。

---

## 7. EAM 落库与幂等

1. 验签通过后尽快返回 2xx。
2. 按 `deliveryId` 去重：已处理过则仍返回 **200**，`duplicate=true`，不再改状态。
3. 用 `originatingEventId` 找登记主日志；找不到再用 `externalDocumentId` + `sourceVersionId`。
4. 每次回调写一条子日志（`Phase=callback`），并回写主日志终态。
5. 本版本不接收 parsing/indexing 中间态。

---

## 8. EAM 响应约定

| HTTP | 含义 | Gateway 应做 |
|---|---|---|
| **200** | 已受理（含幂等重放、缺主日志仍收） | 停止重试 |
| **401** | 验签失败 / 时间戳非法 | **不要重试**（死信） |
| **403** | 回调 secret 未配置 | **不要重试** |
| **5xx** | EAM 内部异常 | 可重试 |

成功体：

```json
{"code":0,"duplicate":false,"message":"retrievable"}
```

重放同一 `deliveryId`：

```json
{"code":0,"duplicate":true,"message":"duplicate"}
```

验签失败：

```json
{"code":"AUTH_SIGNATURE_INVALID","message":"invalid signature"}
```

Gateway 重试建议：最多 8 次，退避 1 / 5 / 30 / 120 / 600 秒。回调失败 **不会** 把已 `ready` 的文档打回失败。

---

## 9. 完整实例

### 9.1 可问答

```http
POST http://192.168.30.31:5105/api/v1/ai/feed/callback
Content-Type: application/json
X-TY-Timestamp: 1755073500
X-TY-Signature: sha256=<hex>
```

```json
{
  "deliveryId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "eventType": "document.terminal",
  "originatingEventId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "status": "retrievable",
  "timestamp": "2026-08-13T08:05:00+08:00",
  "payloadVersion": "1",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "qualityStatus": "passed",
  "retrievable": true,
  "error": null
}
```

### 9.2 失败

```json
{
  "deliveryId": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "eventType": "document.terminal",
  "originatingEventId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "status": "failed",
  "timestamp": "2026-08-13T08:06:00+08:00",
  "payloadVersion": "1",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "qualityStatus": "failed",
  "retrievable": false,
  "error": {
    "code": "DOCUMENT_PARSE_FAILED",
    "message": "PDF 解析失败",
    "retryable": false
  }
}
```

常见 `error.code`：`DOCUMENT_SOURCE_NOT_FOUND`、`DOCUMENT_HASH_MISMATCH`、`DOCUMENT_PARSE_FAILED`、`DOCUMENT_QUALITY_FAILED`、`DOCUMENT_SYNC_FAILED`、`RAGFLOW_UNAVAILABLE`。

### 9.3 待复核

```json
{
  "deliveryId": "c3d4e5f6-a7b8-9012-cdef-123456789012",
  "eventType": "document.terminal",
  "originatingEventId": "FAC-8255-ATT-22-v1-1d9dd084c59e",
  "externalDocumentId": "FAC-8255-ATT-22",
  "sourceVersionId": "v1-1d9dd084c59e",
  "status": "review_required",
  "timestamp": "2026-08-13T08:07:00+08:00",
  "payloadVersion": "1",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "qualityStatus": "review_required",
  "retrievable": false,
  "error": {
    "code": "DOCUMENT_REVIEW_REQUIRED",
    "message": "文档需要人工复核后才能使用。",
    "retryable": false,
    "reasonCodes": ["REQUIRED_CAPABILITY_NOT_PASSED"]
  }
}
```

`review_required` 与 `failed` 一样会带 `error`。`reasonCodes` 为可选机器码（质量门原因）；台账展示优先用 `message`。
---

## 10. 联调检查清单

1. 登记 `202` 后，EAM 日志为「处理中」，不是成功。
2. Gateway 推 `retrievable` → 台账「可问答」，问询入口开放。
3. 同一 `deliveryId` 再推一次 → HTTP 200 且 `duplicate=true`。
4. 错 secret / 错签名 → **401**，Gateway 不再重试。
5. `failed` / `review_required` 不得标成可问答。
6. 查 AI 投喂日志详情，应能看到 `Phase=callback` 子记录和 `deliveryId`。

排查：一直收不到回调，先查 Gateway 是否启用回调、URL 是否配成上面这一条、31:5105 网络是否通。EAM 反复被重试，查是否误返回了 5xx。
