# EAM 问询接口变更说明：消息附件（v2.2 → v2.3）

面向：EAM 开发 / 测试 / 联调负责人  
契约：`integration-openapi-v2` **2.2.0 → 2.3.0**  
正式契约：`contracts/integration-openapi-v2.yaml`  
完整对接：[`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md) §4.2 / §4.2.1

本文只讲相对**已对接问询接口**的增量。投喂 FILE_SHARE / HMAC **不变**。

---

## 1. 一句话

还是这一条 URL：

```http
POST /enterprise/api/v2/conversations/{conversationId}/messages
```

同一地址支持两种 `Content-Type`：

| 场景 | Content-Type | 请求体 |
|---|---|---|
| 无附件（纯文字 / chips） | `application/json` | **与 v2.2 完全相同，不用改** |
| 有附件（可同时有文字，也可只发文件） | `multipart/form-data` | `metadata` + `files` |

鉴权不变：`Authorization: Bearer <JWT>`。**不要加 HMAC 三头。**

---

## 2. 不变（已接问询的代码可继续用）

- 路径、JWT、`clientMessageId` 幂等、`202` 处理中
- 创建会话、GET 会话/历史/引用、chips 点选
- 无附件时的 JSON body：

```json
{ "clientMessageId": "device-message-001", "question": "请说明该设备的维护步骤。" }
```

```json
{ "clientMessageId": "device-message-002", "suggestionId": "maintenance", "contextVersion": 1 }
```

同一个 `conversationId` 里，上一句纯文字、下一句带图，都打这个 URL，只换 `Content-Type`。

---

## 3. EAM 必须改的（仅有附件时）

对话框里选文件、或用户直接 **粘贴 / Ctrl+V 图片**，对 Gateway 都是附件。没有单独的粘贴接口。EAM 把剪贴板图当成文件放进 `files`（常见为 `image/png`，文件名如 `paste.png`）即可。

**不要**把粘贴图写成 JSON `question` 里的 `data:image/...` 或 base64。那只会当普通长文本，不会看图。

有附件时**不要**在 JSON 上加字段，**整段请求改成 multipart**。

```http
POST {BASE_URL}/enterprise/api/v2/conversations/{conversationId}/messages
Authorization: Bearer <JWT>
Content-Type: multipart/form-data
```

两个 part：

| part 名 | 类型 | 说明 |
|---|---|---|
| `metadata` | JSON 字符串 | 必填。`{ "clientMessageId": "...", "question": "..." }`。`question` 可省略（只发文件合法） |
| `files` | 原始文件字节 | 可多个（同名 part 重复）。不要 base64 |

限制：

- 最多 **5** 个文件，单文件 **10MB**
- 第一波 MIME：`image/jpeg`、`image/png`、`text/plain`、`application/pdf`
- `question` 与 `files` 至少有一个
- **chips 禁止带文件**：有 `suggestionId` 时继续走 JSON，不要混 `files`

curl 示例：

```bash
curl -X POST "$BASE/enterprise/api/v2/conversations/$CID/messages" \
  -H "Authorization: Bearer $JWT" \
  -F 'metadata={"clientMessageId":"device-message-010","question":"面板上这个故障码是什么意思？"};type=application/json' \
  -F "files=@photo.png;type=image/png"
```

只发文件（无文字）：

```bash
curl -X POST "$BASE/enterprise/api/v2/conversations/$CID/messages" \
  -H "Authorization: Bearer $JWT" \
  -F 'metadata={"clientMessageId":"device-message-011"};type=application/json' \
  -F "files=@photo.png;type=image/png"
```

---

## 4. 响应与历史（可选适配，无新 URL）

成功 `200` 在原有字段上**多一个可选** `attachments`（仅元数据）：

```json
{
  "conversationId": "conversation-001",
  "clientMessageId": "device-message-010",
  "runId": "run-001",
  "messageId": "assistant-message-001",
  "answer": "从你上传的图片中识别到疑似故障码 E07……",
  "status": "已完成",
  "citations": [],
  "replayed": false,
  "attachments": [
    {
      "attachmentId": "att-001",
      "fileName": "photo.png",
      "mediaType": "image/png",
      "sizeBytes": 245760,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ]
}
```

`GET .../conversations/{conversationId}/messages` 的用户消息同样可带 `attachments[]`。

- **不回**文件字节，**不嵌** `downloadUrl`
- Gateway **不把对话框原件写入对象存储**；只在本次请求内抽文本/看图，处理完丢弃
- EAM 刚传过文件，历史里展示文件名即可；EAM 也不要持久化这份临时文件
- `citations` 仍只来自已投喂知识库，不是附件本身
- 图里识别出的码是「疑似观察」，回答会写成「从你上传的图片中识别到疑似…」，不要展示成「设备当前故障码是 E07」

---

## 5. 新错误码

| HTTP | 何时 | EAM 处理 |
|---:|---|---|
| 413 | 超过 5 个文件，或单文件超过 10MB | 提示用户压缩/少选 |
| 422 | 不支持的类型（如 docx）、JSON 里塞了 `attachments[].content`、chips+文件、无 question 且无 files | 按本文约束改请求 |
| 503 | `ATTACHMENT_STORAGE_UNAVAILABLE` 附件能力关闭 | 提示稍后重试；纯文字 JSON 提问仍可用 |

原有 401 / 403 / 404 / 409 / 202 行为不变。

---

## 6. 不要做

- 不要新开问询 URL，不要直连 RAGFlow
- 不要先调 `POST .../attachments` 再提问（那不是对话框附件）
- 不要在 JSON 里传 base64（`attachments[].content` → 422）
- 不要把粘贴图塞进 `question`（`data:image/...`）；粘贴也走 `files`
- 不要给问询加 HMAC（投喂仍走 v3 HMAC，与本文无关）
- 不要把对话框附件当知识库文档投喂
- 不要在 EAM 侧持久化这份临时文件
- 不要预期 Gateway 提供附件下载；原件不会进 Minio / 对象存储

---

## 7. EAM 改造清单（本变更）

- [ ] 无附件：继续 JSON，回归原提问/chips/续问
- [ ] 有附件：同一 URL 改 `multipart/form-data`（`metadata` + `files`）
- [ ] 对话框粘贴图片与点选上传同一路径：放进 `files`，不要塞进 `question`
- [ ] 只发文件（无文字）可提交
- [ ] 限制 jpeg/png/txt/pdf、最多 5 个、单文件 10MB
- [ ] chips 与文件不同请求
- [ ] 历史/结果可展示 `attachments[].fileName`（不依赖下载 URL）
- [ ] 处理 413 / 422 / 503

Gateway 侧已按 OpenAPI 2.3.0 实现。
