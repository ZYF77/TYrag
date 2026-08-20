# EAM 问询接口变更说明：正文引用角标绑定（v2.7 → v2.8）

面向：EAM 开发 / 测试 / 联调负责人  
契约：`integration-openapi-v2` **2.7.0 → 2.8.0**  
正式契约：`contracts/integration-openapi-v2.yaml`  
完整对接：[`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md) §4.4  
前置说明：[`eam-inquiry-citation-notice.md`](./eam-inquiry-citation-notice.md)（引用过滤与 `downloadUrl`）

本文只讲相对**已对接问询接口**的增量。投喂 FILE_SHARE / HMAC **不变**。问询提问仍用 JWT。无新 URL、无新请求字段。

---

## 1. 一句话

助手正文 `answer` / `content` 里可能出现 `[ID:n]`（或 `[n]`）。EAM 应把合法标记渲染成角标/脚注，并用每条 citation 新增的 **`refIndex`** 绑定到 `citations[]`，**不要**把 `n` 当成数组下标。

---

## 2. 不变（已接问询的代码可继续提问）

- 会话创建、JWT、`clientMessageId` 幂等、chips、纯文字/附件提问路径
- `citations[].citationId` / `title` / `externalDocumentId` / `sourceVersionId` / `pageNo` / `bbox` / `excerpt` / `downloadUrl` / `downloadExpiresAt`
- `status` 与 `citations` 相互独立
- 投喂接口、HMAC、知识库解析均不变
- 忽略未知字段的客户端仍可工作；本变更是 **additive**

---

## 3. Gateway 保证什么

| 保证 | 说明 |
|---|---|
| 正文可含标记 | 知识库事实后常见 `[ID:0]`、`[ID:1]`；纯附件回答 / 无依据拒答通常没有 |
| Gateway 会清洗 | 返回前将可恢复的乱码修成 `[ID:n]`，去掉时间片段里的 `[7]` / 空 `[]` / 无法恢复的残缺括号；EAM 仍应按合法标记 + `refIndex` 渲染 |
| `citations` 只含真正引用 | 与 v2.4 相同；`无可靠依据` 时一定是 `[]` |
| 新增 `refIndex` | 等于正文标记里的 `n`，用于硬绑定 |
| 标记语法 | 推荐只认 `\[(?:ID:)?(\d+)\]`（与 Gateway 一致） |
| 乱码不保证 | 如 `[ID:[[[[` 视为非法，不当引用；可忽略或当普通文本 |

示例（提问 JSON / 历史消息同理）：

```json
{
  "answer": "根据手册，搅拌频率记录见画面说明[ID:2]，维修处理见工单[ID:1]。",
  "status": "已完成",
  "citations": [
    {
      "citationId": "chunk-manual-msg12345",
      "sourceType": "document",
      "title": "运行手册.pdf",
      "externalDocumentId": "EAM-DOC-2",
      "sourceVersionId": "v3",
      "refIndex": 2,
      "excerpt": "……",
      "downloadUrl": "https://{GATEWAY}/enterprise/api/v2/citations/.../file/{ticket}",
      "downloadExpiresAt": "2026-08-20T06:00:00Z"
    },
    {
      "citationId": "chunk-repair-msg12345",
      "sourceType": "document",
      "title": "维修工单.pdf",
      "externalDocumentId": "EAM-DOC-1",
      "sourceVersionId": "v1",
      "refIndex": 1,
      "excerpt": "……",
      "downloadUrl": "https://{GATEWAY}/enterprise/api/v2/citations/.../file/{ticket}",
      "downloadExpiresAt": "2026-08-20T06:00:00Z"
    }
  ]
}
```

注意：`citations[0].refIndex === 2`，**不是** `0`。若误用 `citations[n]`，会绑错或越界。

---

## 4. EAM 必须改的（展示层）

### A. 渲染角标

1. 展示 `answer` / `Message.content` 时，扫描合法标记 `\[(?:ID:)?(\d+)\]`
2. 用 `n` 在本条消息的 `citations` 里查找 `citation.refIndex === n`
3. 命中：把该处替换成角标（¹ / [1] / 上标等），点击打开对应 citation（`downloadUrl` 规则见 citation-notice）
4. 同一 `n` 多次出现 → 同一角标 / 同一 citation
5. 找不到 `refIndex === n`，或 `refIndex` 为 `null` → **不要**瞎绑数组下标；可隐藏该标记或保留原文
6. 非法/残缺标记（括号不配、非数字）→ 不当引用

### B. 历史与 SSE

- JSON 提问结果、`GET .../messages` 历史、SSE `answer.delta` + `citation` 事件：**同一套绑定规则**
- SSE 仍以最终 `answer.completed` / 后续 GET 历史为准做完整渲染亦可
- 不要剥离 Gateway 返回的合法 `[ID:n]` 后再无法绑定；角标替换在 EAM 前端完成

### C. 不要做

- 不要用 `citations[n]` 代替 `refIndex === n`
- 不要假设 `citations` 按 0..k 连续对应正文全部 ID
- 不要因为出现 `[ID:n]` 就改判 `status`
- 不要依赖散文形式 `知识库ID:n`（Gateway 选引用时可能认，但 **角标绑定以 `refIndex` + 方括号标记为准**）
- 不要直连 RAGFlow / MinIO

---

## 5. `refIndex` 为 null 时

极少数多轮场景下，正文标记编号相对本轮检索越界，Gateway 可能用正文重叠回退保留 ≤2 条 citation，此时：

```json
{ "refIndex": null, "citationId": "...", "downloadUrl": "..." }
```

EAM：可在引用面板列出这些 citation，但**不要**把它们挂到某个 `[ID:n]` 角标上。

---

## 6. EAM 改造清单（本变更）

- [ ] 提问 / 历史 / SSE：正文解析合法 `[ID:n]` / `[n]`
- [ ] 用 `citations[].refIndex` 绑定角标，禁止 `citations[n]`
- [ ] 同 `n` 复用同一角标；无匹配或 `refIndex=null` 不瞎绑
- [ ] 非法标记不当引用
- [ ] 角标点击仍走 `downloadUrl`（不带 JWT）
- [ ] 回归：无引用回答、`无可靠依据`、纯附件、web citation（若启用）

Gateway 侧已按 OpenAPI **2.8.0** 实现 `refIndex`。
