# ADR-009 EAM v2 最小 Grounding Guard

- 状态：Accepted
- 日期：2026-08-19
- 修订：2026-08-19（Final Guard 前移到 RAGFlow 持久化前，恢复 Session）

## 决策

EAM 正式问询入口仅在 `/enterprise/api/v2` 建立最小 Grounding Guard 闭环，外部
URL、请求体、响应字段、HTTP 状态和 SSE 事件名保持不变。

在线默认路径：ACL / 设备硬筛 → Hybrid Retrieval + 现有 Rerank → 一次 LLM →
Identifier / Numeric / no-evidence 保险丝 → RAGFlow Session persist → Gateway。

Guard 是保险丝，不是第二套推理。它只检查工单号/设备号是否出现在本请求内部
`effectiveKnowledge`、数字是否有依据（含 `kPa↔MPa` 单位等价）、自算百分比 FAIL，
以及附件观察的窄放行。不理解整段语义，不增加第二次检索或第二模型。

### RAGFlow

仅当内部请求携带 `grounding_version=1` 时：

- 在最终 prompt 中标记知识段；若现有 prompt-fit 截掉知识 START marker，只从知识
  尾部逐块裁剪并重建 prompt；单块仍无法保留 marker 时不调用模型。
- `effectiveKnowledge` 只留在 RAGFlow 内部，用于 Final Guard。对外响应不再返回
  `grounding` / `effectiveKnowledge`。
- Guard 在 `decorate_answer` 之后、Session 持久化之前执行。PASS 原样进入 Session；
  FAIL 且仍有 `effectiveKnowledge` 时，同批证据短答重试一次后再过同一保险丝；仍 FAIL
  或无证据改为标准拒答 `未找到可靠依据，无法回答。`，`reference.chunks=[]`，
  无 think。幻觉候选不得写入 `conv.message`。
- grounding 请求禁止 yield candidate token。未携带版本的 RAGFlow UI/API 保持原行为。
- `dialog_service.py` 不得出现 `equipmentId` 或 EAM 状态枚举。
- 同一内部请求不记录完整问题、prompt/history、知识正文或模型输出。

### Gateway

Gateway 负责认证、ACL、设备硬筛、附件、`internetEnabled`、EAM 契约、业务库历史 /
幂等 / citation。v2 创建并复用 `ragflow_session_id`，请求体为
`question + session_id + doc_ids + grounding_version=1 + allowed_identifiers`，
不再投影 `messages`，不再要求跨服务 `effectiveKnowledge`，不再在 Gateway 上跑
Final Guard。旧会话 `ragflow_session_id` 为 NULL 时下一轮新建，不回填。

绑定会话的 `equipmentId` / `fixedAssetNo` 以及用户本轮问题中已出现的设备号视为
身份而不是新事实，作为 `allowed_identifiers` 传给保险丝。当前附件只能支持明确
标为“附件可见/显示/识别/疑似”的观察。

SSE 仍先发送 `run.started`，缓存 RAGFlow 终态后再发送一个安全的 `answer.delta`。
Guard 前不发送 `answer.delta` 或 `reasoning.delta`；等待期间可发送标准 SSE
comment heartbeat。

设备号召回策略与 Guard 解耦。本工作包保留现有 ACL 后的设备 metadata 硬过滤。

## 路线图

| 阶段 | 内容 |
|---|---|
| **P0（本轮）** | `effectiveKnowledge` 留在 RAGFlow 内部；Pre-Persist Identifier/Numeric Guard；no-evidence abstain；Session / MinIO 修复；Retrieval baseline 记录（不调参） |
| **P1** | 白名单 Query Adapter 进问询（维修次数等走 PG）；query-aware equipment policy；Attribution Guard |
| **P2** | 高风险题 Selective Verifier；句子级增量流式；有 baseline 之后才允许 Retrieval/Rerank 调参 |

## 首版明确不做

| 延期项 | 重新评估条件 |
|---|---|
| Pre-Gate `/retrieval` | 生成成本成为实测瓶颈，且能复用同一份冻结证据，避免二次检索不一致 |
| `visibleChunkIds` | 运营确实需要 chunk 级可追溯，且能从最终 prompt 保持可靠映射 |
| sentinel escape | 随机 nonce 重生成仍出现实测碰撞 |
| formal/v1 接入 | 确认仍有生产调用方 |
| 独立跨服务健康门禁 | 分阶段部署需要独立 readiness 保证 |
| 设备号加权/授权范围扩展 | 与 Attribution Guard、prompt 调整和跨设备误归属评测同一工作包交付 |
| 语义验证器/来源权威校验 | 剩余错误主要是无数字的陈述、矛盾或来源归属 |
| 派生计算器 | 业务明确要求证据中不存在的换算或计算 |
| 增量安全流式 | 完整回答缓存造成的首字延迟不可接受 |
| Gateway 历史压缩 | 实测上下文大小或延迟超限；摘要本身必须可追溯 |
| Retrieval/Rerank 调参 | P0 baseline 之后，且有标注集才允许；Recall 本轮保持 `null` |
| 在线 LLM-as-judge | Faithfulness / Recall@K 留在离线评测 |

## 后果

- EAM 不需要改接口；需要允许现有 Gateway 最大生成超时内没有 answer token，并允许
  标准 SSE comment heartbeat。
- RAGFlow UI 和未携带 `grounding_version=1` 的直接调用保持原行为。
- 首版只约束标识符、数值和附件观察，不声称解决无数字的语义幻觉、矛盾或来源归属。
- 绑定设备后只有发票类文档时，编造维修史仍拒答——精确次数要等 P1 Query Adapter。
- 回滚 RAGFlow 保险丝补丁前必须同时回滚 Gateway 的 `grounding_version=1` 请求；
  补丁按 `patches/CHANGE-REQUEST-EAM-V2-GROUNDING-CONTEXT.md` 独立重放或删除。
