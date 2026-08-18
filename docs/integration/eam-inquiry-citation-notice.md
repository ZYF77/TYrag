# EAM 问询接口变更说明：引用过滤与统一下载（v2.3 → v2.4）

面向：EAM 开发 / 测试 / 联调负责人  
契约：`integration-openapi-v2` **2.3.0 → 2.4.0**  
正式契约：`contracts/integration-openapi-v2.yaml`  
完整对接：[`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md) §4.4

本文只讲相对**已对接问询接口**的增量。投喂 FILE_SHARE / HMAC **不变**。问询提问仍用 JWT。

---

## 1. 一句话

`citations[]` 只保留回答真正用到的引用。每条引用多一个 `downloadUrl`：短时 ticket，**下载不带 JWT**。EAM 不区分裁剪图/原件，按 HTTP `Content-Type` 自己渲染。

---

## 2. 不变（已接问询的代码可继续提问）

- 会话创建、JWT、`clientMessageId` 幂等、chips、纯文字/附件提问路径
- `citationId` / `title` / `externalDocumentId` / `sourceVersionId` / `pageNo` / `bbox` / `excerpt`
- `status` 与 `citations` 相互独立，禁止用空引用改判状态
- 投喂接口、HMAC、知识库解析均不变

---

## 3. EAM 必须改的

### A. 引用列表变少、变准

- 以前：检索命中的 chunk 可能整表出现在 `citations`
- 现在：只有回答正文里真正标了引用的 chunk 才会出现（含 `[ID:n]`，以及模型偶发写出的 `知识库ID:n` / `ID:n` 散文）
- 多轮问答中模型若沿用上一轮 `ID:n` 导致编号越界：仅当本轮 chunk 正文与回答有实质重合时才保留（最多 2 条），避免把无关检索页挂成引用
- `status = 无可靠依据` 时 `citations` 一定是 `[]`（即使回答有字）
- 检索内容无法支撑用户当前所问事实（例如问「有没有维修记录」但只有合格证/调试记录）→ `status = 无可靠依据`，`citations = []`，不要弹出对照文档
- 半支撑（例如「有开箱验收单，但缺验收人姓名」）→ `已完成` + 应保留对验收单等支撑文档的引用
- 问「有哪些资料」且概括真实命中文档 → 仍可 `已完成` + 真实引用
- 不要再假设「这个设备投喂过的文件都会出现在 citations」

### B. 用 `downloadUrl` 打开引用文件，不要再接 JWT source

提问响应 / 历史 / citation 详情的每条 citation 新增：

```json
{
  "citationId": "cite-001",
  "title": "维修工单.pdf",
  "externalDocumentId": "EAM-DOC-001",
  "sourceVersionId": "v3",
  "pageNo": 2,
  "bbox": null,
  "excerpt": "……",
  "downloadUrl": "https://{GATEWAY}/enterprise/api/v2/citations/cite-001/file/{ticket}",
  "downloadExpiresAt": "2026-08-17T04:30:00Z"
}
```

```http
GET {downloadUrl}
```

- **不要**带 `Authorization`
- **不要**区分裁剪图还是原件；Gateway 内部决定返回哪种字节
- 看响应头 `Content-Type` 渲染：常见 `image/png` / `image/jpeg`（裁剪图）或 `application/pdf`（原件）
- `downloadExpiresAt` 过期后重新 GET 消息/citation 详情拿新 URL；不要缓存过期 ticket
- 过期、错票、跨用户、无权 → HTTP `404`；提示用户重新打开引用

---

## 4. 新错误（仅下载口）

| HTTP | 何时 | EAM 处理 |
|---:|---|---|
| 404 | ticket 过期 / 用过次数超限 / 不是这条 citation / 当前用户无权 | 重新拉消息拿新 `downloadUrl`；不要重放旧 URL |
| 404 | citation 已不存在或不属于当前用户 | 不要猜 ID |

提问接口原有 401 / 403 / 404 / 409 / 202 行为不变。下载口**不**要 JWT，拿不到文件就是 404，不要当成 401 去补 Token。

---

## 5. 不要做

- 不要继续把 `GET /citations/{id}/source`（JWT PDF）当主路径写进新改造
- 不要期望响应里出现 `imageId`、`chunkId`、`sourceDownloadUrl`、`imageDownloadUrl`
- 不要直连 MinIO / 对象存储 / RAGFlow
- 不要在 31 服务器存裁剪图
- 不要给下载 URL 再加 HMAC 或 JWT
- 不要因为 `citations` 为空就把 `status` 改判成无依据（或反过来）
- 投喂接口不要为这次改任何字段

---

## 6. EAM 改造清单（本变更）

- [ ] 提问/历史/citation 详情读取 `citations[].downloadUrl` + `downloadExpiresAt`
- [ ] 点击引用：对 `downloadUrl` 做 GET，**不带 JWT**，按 `Content-Type` 预览或下载
- [ ] `无可靠依据` 时按空引用展示，不要把设备下所有投喂文件或对照文档列出来
- [ ] ticket 404 / 过期：重新 GET 消息或 citation 详情换新 URL
- [ ] 不要解析或依赖裁剪图/原件两种 URL
- [ ] 回归：无附件 JSON 提问、chips、附件 multipart 均不受影响

Gateway 侧已按 OpenAPI 2.4.0 实现。
