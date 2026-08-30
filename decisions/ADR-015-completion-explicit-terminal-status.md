# ADR-015 completion 显式业务终态

- 状态：Accepted
- 日期：2026-08-30
- 关联：RF-PATCH-007；ADR-009 / ADR-010（Grounding Guard）；`docs/integration/eam-grounding-guard-overview.md`

## 背景

Enterprise Gateway 此前在 RAGFlow 未显式给出业务状态时，只能靠「回答是否为空、
有无 chunks、是否命中拒答措辞正则」推测 `completed` / `no_reliable_evidence` /
`failed`。这类启发式会把模型正文里的拒答短语、Gateway 自己的改写当成状态信号，
也违反「消息业务状态与 citations 证据数据解耦」的项目原则。

业务终态只有运行结果本身能决定，必须在产生回答的地方（RAGFlow completion 链路）
显式输出。

## 决策

RAGFlow 在 chat completion 的 `data` 对象内新增显式字段 `status`，取值仅：

- `completed`：正常回答；
- `no_reliable_evidence`：无可靠依据的终态拒答；
- `failed`：流式过程中的异常错误帧（仅 `chat_api.py` 的 `code: 500` 帧）。

判定规则（只有精确判断，不做任何正则/模糊措辞判断）：

1. grounding 弃答事件（`_grounding_abstain_event`：空知识 empty_response 命中、
   prompt-fit 单块装不下、流式最终答案为空）→ `no_reliable_evidence`；
2. Guard 融合弃答（`_fuse_or_keep` fused 分支把 answer 换成标准拒答文案）→
   由精确相等判断覆盖 → `no_reliable_evidence`；
3. 最终答案精确等于 `STANDARD_ABSTAIN_ANSWER`（`未找到可靠依据，无法回答。`，
   允许首尾空白）或最终答案为空白 → `no_reliable_evidence`；
4. 其余 → `completed`。

输出位置：

- JSON 非流式响应：最终 `data` 携带 `status`；
- SSE 流式：`final: True` 的最终帧 `data` 携带 `status`；非 final 的 delta 帧不携带；
- `chat_api.session_completion` 流式 except 的 `code: 500` 错误帧 `data` 携带
  `"status": "failed"`。

> 备注：`code: 500` 错误帧的 `data.status="failed"` 目前不会以 payload 形式到达
> Gateway——Gateway 客户端（`enterprise/gateway/query/ragflow_client.py` 的
> `chat_completion_stream`）对 `code != 0` 的帧直接抛 `RAGFlowAPIError` 走
> `run.failed`，结果等价 fail-closed。保留该字段是为了契约完整与未来放行。

实现落在 `dialog_service._completion_status()` 一个纯函数上，在 completion 可达的
最终事件（`async_chat_solo` / `async_chat` / `rag_agent` 的流式与非流式终帧、
grounding 弃答事件）统一附加。`code/message/data` 既有字段语义不变；
`async_ask` 等非 completion 链路不改。

## 替代方案

| 方案 | 结论 |
|---|---|
| 维持 Gateway 启发式（正文正则 + chunks 判空） | 已证实会误判，且状态与证据解耦原则不允许按 citations 推导状态，否决 |
| Gateway 调用后按回答文本二次分类 | 与方案一相同，只是换了个位置猜，否决 |
| 在 `code/message` 之外新增顶层字段 | completion 响应已固定 `code/message/data` 三层，字段放进 `data` 最小侵入，否决顶层 |
| RAGFlow 全链路（含 `async_ask`、UI 会话）统一输出 | 超出本次需求；`async_ask` 消费方无业务状态诉求，留待后续 |

## 兼容风险

- 新增字段为纯增量：RAGFlow UI、OpenAI 兼容层、channels 等 `async_chat` 其他消费方
  会看到多出的 `status` 键，它们只读取已知字段，不受影响。
- 非流式 `data` 与流式 final 帧都会带 `status`；Gateway 之外若有人对响应做严格
  schema 校验（拒绝未知字段），需要同步放行该字段。
- 未携带 `grounding_version` 的普通请求同样会得到 `status`；其中非 grounding 流式的
  终帧 `answer` 仍为空串（上游既有行为，正文已随 delta 流出），`status` 按流出的
  正文判定。
- `async_chat_solo` 非 grounding 流式路径上游本就不产出 final 帧，本补丁不为其
  合成新帧；该场景 `status` 缺失，由调用方按无终态处理。

## 回滚

按 `patches/CHANGE-REQUEST-RF-PATCH-007-COMPLETION-STATUS.md` 独立重放或删除：

- 删除 `dialog_service.py` 中 `_completion_status` 及各终帧上的 `status` 赋值、
  `_grounding_abstain_event` 内的 `status` 键；
- 删除 `chat_api.py` 错误帧 `data` 内的 `"status": "failed"`；
- 回滚后 Gateway 不得继续读取 `status` 字段（需与 Gateway 侧开关同步回退）。
