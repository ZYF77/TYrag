# EAM 问询接口变更说明：思考过程与正文拆分（v2.4 → v2.5）

面向：EAM 开发 / 测试 / 联调负责人  
契约：`integration-openapi-v2` **2.4.0 → 2.5.0**  
正式契约：`contracts/integration-openapi-v2.yaml`  
完整对接：[`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md) §4.2

本文只讲相对**已对接问询接口**的增量。投喂 FILE_SHARE / HMAC **不变**。问询提问仍用 JWT。

---

## 1. 一句话

助手回复拆成两段：`answer` / `content` **只给用户看的正文**；可选 `reasoning` 是模型思考过程。EAM 有值时做成可展开「思考中」，不要把思考混进气泡正文。

---

## 2. 不变（已接问询的代码可继续提问）

- 会话创建、JWT、`clientMessageId` 幂等、chips、纯文字/附件提问路径
- `status`、`citations`、`downloadUrl` 语义与 v2.4 相同
- 提问请求 JSON / multipart **不用改字段**
- 投喂接口、HMAC、知识库解析均不变
- 只读 `answer`、忽略未知字段的旧客户端仍能提问；思考不再出现在正文里

---

## 3. EAM 必须改的

### A. 正文不再包含思考过程

以前：`answer` / 历史 `content` 可能是「规划文字 + 真正回复」一整段。

现在：

| 字段 | 谁用 | 内容 |
|---|---|---|
| `answer`（提问 JSON）/ `content`（历史） | 气泡正文 | 只给用户看的最终文字 |
| `reasoning` | 可展开「思考中」 | 思考过程；没有则为 `null` |

```json
{
  "answer": "你好呀😊！如果你需要查询设备台账信息……",
  "reasoning": "用户再次打招呼，按照之前的回复风格……",
  "status": "已完成",
  "citations": []
}
```

- 用户消息：`reasoning` 一定是 `null`
- 旧历史（本版本上线前）：`reasoning` 为 `null`，Gateway **不回扫**旧消息
- 没有 `<think>` 标签的规划文字仍可能留在 `answer`（Gateway 不做启发式切割）

### B. UI 建议

- 有 `reasoning`：先显示「思考中」，点击展开全文；正文只渲染 `answer` / `content`
- 无 `reasoning`：只显示正文，不要画空的思考区
- 引用 `citations`、设备号提示只跟正文走，不要用思考过程去匹配引用

### C. SSE（首期仍可不接）

若已接 `Accept: text/event-stream`：

```text
run.started
  → 0..n × (reasoning.delta | answer.delta | citation)
  → answer.completed  或  run.failed
```

- 思考 token → `reasoning.delta`
- 正文 token → `answer.delta`（语义收窄为仅正文）
- 不接 SSE 的客户端继续用 JSON 即可

---

## 4. 不要做

- 不要把 `reasoning` 拼回 `answer` 再展示
- 不要用 `reasoning` 是否为空改判 `status` 或清空 `citations`
- 不要期望思考过程里的 `[ID:n]` 产生引用
- 不要为这次改投喂 / HMAC / citation ticket
- 不要让 Gateway 画 EAM 前端组件

---

## 5. EAM 改造清单（本变更）

- [ ] 提问 JSON 读取 `reasoning`；有值则做可展开「思考中」
- [ ] `GET .../messages` 历史同样读 `content` + `reasoning`；用户消息 `reasoning` 为 `null`
- [ ] 气泡正文只渲染 `answer` / `content`
- [ ] 旧客户端若暂不改 UI：忽略 `reasoning` 仍可用，但看不到思考（预期）
- [ ] 已接 SSE：把 `reasoning.delta` 与 `answer.delta` 分开渲染
- [ ] 回归：无附件 JSON、chips、附件 multipart、citation `downloadUrl` 均不受影响

Gateway 侧已按 OpenAPI 2.5.0 实现。
