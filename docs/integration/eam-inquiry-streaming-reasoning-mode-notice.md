# EAM 问询接口变更说明：真流式与推理档位（v2.8 → v2.9）

面向：EAM 开发 / 测试 / 联调负责人  
契约：`integration-openapi-v2` **2.8.0 → 2.9.0**  
正式契约：`contracts/integration-openapi-v2.yaml`  
完整对接：[`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md)

本文只讲相对**已对接问询接口**的增量。投喂 FILE_SHARE / HMAC **不变**。问询提问仍用 JWT。

---

## 1. 一句话

提问 JSON / multipart 可带 `reasoningMode`。SSE 恢复边生成边推 `reasoning.delta` 和 `answer.delta`。若 Gateway 在终态改写了已流出的正文，会再发一帧 `answer.replaced`，EAM **必须用它整体替换气泡正文**。

---

## 2. 不变

- 会话创建、JWT、`clientMessageId` 幂等、chips、纯文字/附件提问路径
- JSON 提问不带 `reasoningMode` 时行为与今天一致（`simple`）
- `status`、`citations`、`downloadUrl`、`refIndex` 语义不变
- `answer.completed` **仍然不含** `reasoning`；思考过程看 `reasoning.delta` 或 JSON 历史里的 `reasoning`
- 不接 SSE 的客户端继续用 JSON 即可

---

## 3. EAM 可选改：`reasoningMode`

请求字段（question / suggestion / multipart metadata 均可）：

| 值 | 含义 | 成本/时延 |
|---|---|---|
| `simple`（默认） | 一轮普通 chat，不传 RAGFlow `reasoning` | 最低 |
| `low` | RAGFlow reasoning=1 | 较低 |
| `medium` | RAGFlow reasoning=2 | 中 |
| `high` | RAGFlow reasoning=3 | 高 |
| `ultra` | RAGFlow reasoning=4 | 最高 |

非法值返回 **422**。省略字段等于 `simple`。旧客户端不传该字段，继续走 `simple`，**不用改请求**。

```json
{
  "clientMessageId": "msg-001",
  "question": "西门子的运行信息有哪些",
  "reasoningMode": "medium"
}
```

`high` / `ultra` 只有在本轮 `internetEnabled=true` 且聊天已配置联网检索时才会绑定 `web_search`。默认不要开联网。

档位主要提升理解与综合证据的能力，**不能保证不幻觉**。先在同一批真实问题上比较 `medium` / `high` / `ultra` 再固定生产默认值。

---

## 4. EAM 必须改（若已接 SSE）

### A. 事件顺序

```text
run.started
  → 0..n × reasoning.delta
  → 0..n × answer.delta
  → 0..1 × answer.replaced
  → 0..n × citation
  → answer.completed  或  run.failed
```

思考与正文现在是真流式，不再等整段生成完才推一帧。EAM 已接 SSE 时必须按上面顺序消费，**不能再假设只有一个 `answer.delta`**。

示例（终态未改写，无 `answer.replaced`）：

```text
event: run.started
data: {"conversationId":"...","clientMessageId":"...","runId":"...","replayed":false}

event: reasoning.delta
data: {"conversationId":"...","runId":"...","content":"先看运行手册再归纳"}

event: answer.delta
data: {"conversationId":"...","runId":"...","content":"根据资料，"}

event: answer.delta
data: {"conversationId":"...","runId":"...","content":"运行信息包括……[ID:0]"}

event: citation
data: {"citationId":"...","refIndex":0,...}

event: answer.completed
data: {"conversationId":"...","runId":"...","messageId":"...","status":"已完成","citations":[...]}
```

### B. `answer.replaced`

```json
{
  "conversationId": "...",
  "runId": "...",
  "content": "未找到可靠依据，无法回答。"
}
```

语义：**丢弃本轮已经拼进气泡的全部 `answer.delta`，改用本帧 `content`。**

典型原因：引用标记清洗、软拒答改成标准拒答句、资料清单救援、补充设备号提示。

`reasoning.delta` 没有替换事件。

### C. 回放

同一 `clientMessageId` 的成功回放：

- 只有合并后的单帧 `reasoning.delta`（若有）和单帧 `answer.delta`
- **不发** `answer.replaced`
- `answer.delta.content` 已是终态正文

### D. JSON

非 SSE 路径仍一次返回终态 `answer` / `reasoning` / `status` / `citations`，没有 `answer.replaced`。

---

## 5. 不要做

- 不要把 `reasoning.delta` 拼进气泡正文
- 不要用 `citations` 是否为空改判 `status`
- 不要期望 `answer.completed` 带 `reasoning`
- 不要在回放里再等 `answer.replaced`
