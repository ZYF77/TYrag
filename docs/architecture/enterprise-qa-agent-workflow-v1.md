# Enterprise QA Agent Workflow v1

> Companion architecture note for the Agent Workflow second Harness entry.
> Full combined design: `docs/architecture/tyrag-agent-workflow-and-ingestion-v1.md`.
> ADR: `decisions/ADR-021-Agent-Workflow管控范围与多格式Pipeline.md`.
> Patch: `RF-PATCH-012`.

## Scope

- Harness second entry: **Agent Workflow 测试** (`workflow` mode).
- Gateway routes: `POST /enterprise/api/v1/workflow/conversations/{id}/messages`, `GET /enterprise/api/v1/workflow/status`.
- No silent Chat fallback when Workflow is enabled but unconfigured (`WORKFLOW_NOT_CONFIGURED`).
- Session fields: `workflow_agent_id`, `workflow_version`, `workflow_session_id` (isolated from Chat `ragflow_session_id`).
- Version apply via `ENTERPRISE_WORKFLOW_ENABLED` / `ENTERPRISE_WORKFLOW_AGENT_ID` / `ENTERPRISE_WORKFLOW_VERSION`.

## Hard constraints

1. Retrieval uses `doc_scope_mode=restrict` and `doc_scope_ids=["begin@authorized_doc_ids"]`.
2. Missing/empty Gateway scope → empty retrieval (never corpus-wide).
3. Post-TOC / parent-child expansion is re-filtered to the same document ceiling.
4. Begin inputs use the public `{"type","value"}` contract so UserFillUp does not drop business fields.
5. Gateway injects authorized scope G, reads current-user Memory, owns citation/status/SSE/history.
6. Model must not invent `doc_ids`, dataset, tenant, external ids, paths, or unsigned URLs.


## Template v1.2 ↔ Chat v12 alignment

Checked-in canvas remains `enterprise/workflows/enterprise_qa_agent_v1.template.json`
(historical v1.2 note; **current** is v1.4 — see Template v1.4 section). **No large topology change** vs v1.1.

Hard alignment with production enterprise Chat v12 (`enterprise_identity_metadata_v12` kept on Chat; this Workflow does not PATCH Chat `prompt_config`):

- User-visible abstain MUST be: **「当前检索结果中没有找到可靠依据」** (FORBIDDEN: 「未找到可靠依据，无法回答。」).
- Sources: attachment observations + KB Content; `business_context` / `user_memory` for disambiguation/expression only — not device ledger evidence.
- `equipment_id` / `fixed_asset_no` metadata = ownership only; metadata ≠ facts; no field grafting; whole-machine vs parts hierarchy.
- Attachments alone cannot prove enterprise ledger facts; refuse only when the asked core fact lacks evidence.
- No count / numbered lists in summaries; use `[ID:n]` when citing KB Content; FinalGuard strips sentinels so users never see internal reasoning.
- Auth: G = ACL full set; every Retrieval `doc_scope_mode=restrict` + `begin@authorized_*`; no device hard doc filter inside Workflow.

Orchestration deltas vs v1.1:

- Categorize hard priority: **Boundary → Clarify → Complex → Direct**; `MISSING != NONE` → Clarify; single-device single-field must stay Direct (not Complex).
- QueryRefiner keeps stable `QUERY:` + `MISSING:` two-line contract; rewrite never changes auth.
- AdaptiveResearch short framework; forced QueryRefiner QUERY; stop when enough; same sentinel layer as Direct (`[EVIDENCE_OK]` / `[NO_RELIABLE_EVIDENCE]`).
- Clarifier prefers resolving MISSING; if unique `business_context` object is already present, do not re-ask equipment id.
- FinalGuard: NO_RELIABLE or uncertain → v12 abstain; strip sentinels without adding facts.


## Template v1.3 ↔ Soft device pin (Chat v12 abstain kept)

Checked-in canvas remains `enterprise/workflows/enterprise_qa_agent_v1.template.json`
(title / `workflow_version` = `enterprise-qa-agent-v1.3`). **No topology change** vs v1.2.

Deltas vs v1.2 (prompts/contracts only):

- **QueryRefiner soft pin**: if `business_context` has `equipment_id` / `fixed_asset_no` / `model`, rewritten `QUERY` must keep those exact ids (retrieval guidance only — never mutate `authorized_*`; G stays ACL full set).
- **Categorize**: with business equipment id present, single-device single-field stays **Direct** even when G is large (~hundreds of docs); do not route to Complex merely because “lots of knowledge”.
- **FocusedAnswer / AdaptiveResearch soft device gate**: prioritize target-device evidence; forbid answering/citing other `equipment_id` facts (C6 no graft); only other-device evidence → v12 abstain; reliable target evidence → must `[EVIDENCE_OK]`.
- **Clarifier / Boundary**: Clarify asks real 1–2 questions (not Direct abstain sentence); Boundary states 「需要结构化统计/写操作，当前只读 Workflow 做不到」 and must not share Direct’s no-evidence abstain.
- **FinalGuard**: `[NO_RELIABLE_EVIDENCE]` → exact v12 phrase; strip sentinels; block cross-device upgrade.
- User-visible abstain remains **「当前检索结果中没有找到可靠依据」**; legacy abstain string must not appear in the template.
- Gateway formal fix (separate code): `RAGFlowAgentClient.timeout` no-op setter so parent `__init__` assignment does not AttributeError; getter still reads `workflow_timeout_seconds`.

## Template v1.4 ↔ Soft Device Gate rewrite (noise-tolerant; Chat v12 abstain kept)

Checked-in canvas remains `enterprise/workflows/enterprise_qa_agent_v1.template.json`
(title / `workflow_version` = `enterprise-qa-agent-v1.4`). **No topology change** vs v1.3.
**Does not** hard-narrow G: `authorized_doc_ids` stays ACL full set; device remains soft `business_context` only.

Deltas vs v1.3 (prompts/contracts only; eval 2026-09-15 Gateway+Agent v1.3 was 1/10 PASS):

- **QueryRefiner**: stronger soft pin — `QUERY` must carry `equipment_id`; prefer equipment+field subqueries for wide-G retrieval bias (still never mutates `authorized_*`).
- **Categorize**: Clarify/Boundary priority + examples strengthened so missing equipment / plant-wide stats / create work-order do not mis-enter Direct then false-refuse.
- **FocusedAnswer / AdaptiveResearch Soft Device Gate rewrite**: if target device has reliable Content → must `[EVIDENCE_OK]`; **forbid** `NO_RELIABLE` merely because G has other-device noise; ignore noise docs; if wrong-device cites appear → drop bad cites (or refuse only when target evidence vanishes); still forbid grafting other-device facts onto the target.
- **FinalGuard**: must pass through `[CLARIFY]` / `[BOUNDARY]` / `[EVIDENCE_OK]` (strip markers only); map to v12 abstain **only** for `[NO_RELIABLE_EVIDENCE]` / empty / unparseable — never rewrite Clarify/Boundary to abstain.
- **Retrieval description**: within G, soft-prefer chunks matching `metadata.equipment_id` (preference only; auth list unchanged).
- User-visible abstain remains **「当前检索结果中没有找到可靠依据」**; true no-evidence (S7) must still refuse.

## Template v1.1 orchestration (refined)

Historical notes for the refined four-way canvas. **Current checked-in template is v1.4**
(`enterprise-qa-agent-v1.4`); see **Template v1.4 ↔ Soft Device Gate rewrite**, **Template v1.3 ↔ Soft device pin**, and **Template v1.2 ↔ Chat v12 alignment** above for abstain copy and
Categorize priority. Topology is unchanged:

```text
Begin → QueryRefiner → Categorize(QueryRouter)
  Direct  → Retrieval:FocusedEvidence → FocusedAnswer ─┐
  Complex → AdaptiveResearch (+ embedded ResearchEvidence) ─┤
  Clarify → Clarifier ─────────────────────────────────────┤
  Stats/Action → Boundary ─────────────────────────────────┤
                       → AnswerMux → FinalGuard → Message:FinalAnswer
```

Design notes carried into v1.2:
- Four-way Categorize keeps EAM equipment inquiry routes; handles are stable (`route-direct-qa` …).
- AdaptiveResearch is bounded (max_rounds=5) and must not expand authorization; Research Retrieval shares the same restrict hard boundary as Focused Retrieval.
- FocusedAnswer / FinalGuard refuse with Chat v12 fixed copy: 当前检索结果中没有找到可靠依据
- Boundary explains full-corpus stats vs write actions; never Top-N fake stats.
- Clarifier asks at most 1–2 questions; prefers business/asset/attachment context; resolves MISSING first.
- Tool:ResearchRetrieval on the canvas is display-only; runtime uses Agent embedded tools.
- Default llm_id is VolcEngine 豆包 `ep-20260310093543-zl952@LLM@VolcEngine` (no DeepSeek).
- Optional Begin fields gateway_context / asset_relation_context / attachment_context are compatibility-only; Gateway core injects authorized_* / doc_scope_mode / business_context / user_memory / internet_enabled / reasoning_mode / workflow_version.

## Local artifacts

- `enterprise/gateway/query/workflow_router.py`
- `enterprise/gateway/query/workflow_client.py`
- `enterprise/workflows/enterprise_qa_agent_v1.template.json`
- `enterprise/scripts/prepare_workflow_agent.py`
- `enterprise/scripts/validate_workflow_artifacts.py`
- Unit/static: `enterprise/tests/test_workflow_artifacts.py`, `ragflow/test/unit_test/agent/tools/test_retrieval_scope.py`

## Deploy / E2E (out of this finish)

Build RF image with RF-PATCH-012, import agent on 8080, then run formal E2E matrix on server 30 after parse jobs finish. See combined doc §9 and `docs/integration/update-30-server-agent.md`.

## Extracted detail from combined doc

The sections below are the Workflow-specific design already accepted in the combined architecture document.

## 4. 第二测试入口、会话与版本

### 入口

Harness 增加“Agent Workflow 测试”导航项，复用现有聊天、附件、引用和运行日志界面。前端请求 Gateway 的：

```text
POST /enterprise/api/v1/workflow/conversations/{conversation_id}/messages
GET  /enterprise/api/v1/workflow/status
```

Gateway 再调用固定配置的 RAGFlow：

```text
POST /api/v1/agents/chat/completions
```

客户端不能传入任意 `agent_id`。服务端通过部署配置固定：

```text
ENTERPRISE_WORKFLOW_ENABLED=true
ENTERPRISE_WORKFLOW_AGENT_ID=<applied-agent-id>
ENTERPRISE_WORKFLOW_VERSION=enterprise-qa-agent-v1.4
```

未配置固定 ID 或版本时，入口返回 `WORKFLOW_NOT_CONFIGURED`，不回退到 Chat。

### 会话隔离

- Chat 与 Workflow 使用命名空间不同的幂等键；一个入口不会重放另一个入口的结果。
- `ext_v2_conversation` 保存 `workflow_agent_id`、`workflow_version`、`workflow_session_id`，与 Chat 的 `ragflow_session_id` 分开。
- 已有 Workflow 会话绑定版本不一致时拒绝继续；编辑草稿不影响已应用版本。
- 运行输入每轮重新计算，不能复用上一轮的授权范围。
- 应用版本后创建的新测试会话绑定该版本；旧会话继续使用旧版本，便于回滚和对照。


## 5. Enterprise QA Agent Workflow v1 / v1.1 / v1.2 / v1.3 / v1.4

### 主流程

```text
Gateway
  ├─ authenticate / ownership / ACL
  ├─ calculate authorized scope G
  ├─ read current-user Memory
  └─ secure attachment observation/upload
        ↓
Begin
  ├─ query / history / business context
  ├─ authorized_dataset_ids
  ├─ authorized_doc_ids
  ├─ attachment context
  └─ user_memory
        ↓
Agent
  ├─ Knowledge Retrieval (restricted)
  ├─ optional bounded re-query
  ├─ optional WebSearch when explicitly allowed
  ├─ evidence sufficiency
  └─ final answer with [ID:n] markers
        ↓
Message
        ↓
Gateway
  ├─ citation authorization and external mapping
  ├─ explicit terminal status
  ├─ JSON/SSE/history persistence
  └─ async user-memory candidate after successful persistence
```

首版画布模板位于 `enterprise/workflows/enterprise_qa_agent_v1.template.json`，文件采用 RAGFlow Agent 导入器要求的 canonical DSL：`graph.nodes`、`graph.edges` 和 `components` 位于顶层；不能再把它包进额外的 `dsl` 对象。模板包含：

- Begin 的授权 dataset/document 输入、业务上下文、附件和 Memory 输入；
- Agent 的 Retrieval 工具，使用 `begin@authorized_dataset_ids` 和 `begin@authorized_doc_ids`；
- v1 默认不配置 Web Search；联网版本需单独评审，真实 provider key 不进入 Git；
- 强制引用格式、无可靠证据和禁止生成内部安全字段的提示；
- Message 流式输出。

模型和工具预算初始沿用现有五档：`simple/low/medium/high/ultra` 的检索上限为 `1/1/2/3/4`，联网每次运行最多一次。联网默认关闭；首版不开放 SQL、任意 HTTP/MCP、KG、TOC 和导航工具。

### 证据与终态

Workflow 产出的是证据候选和回答；Gateway 负责把引用映射为外部安全投影。空证据、工具错误和模型错误分别处理：

```text
completed             有可靠证据且完成回答
no_reliable_evidence  运行成功但证据不足
failed                工具、模型、协议或运行失败
```

`citations` 与业务状态相互独立，不能用其中一个推导另一个。没有可靠业务证据时，用户 Memory 也不能把状态改成 `completed`。

保留 `run.started`、`answer.delta`、`answer.replaced`、`citation`、`answer.completed`、
`run.failed` 和取消语义；兼容解析 `reasoning.delta`，但不转发 Canvas 的原始思维过程。
Canvas 的节点 trace、Prompt、原始 chunks 和附件正文不进入公共诊断。

### 原生 Retrieval 的硬约束

`RF-PATCH-012` 为 `RetrievalParam` 增加：

```text
doc_scope_ids
doc_scope_mode
```

`doc_scope_ids` 可以是 Gateway 注入的画布引用，例如 `begin@authorized_doc_ids`；`restrict` 模式下：

- scope 缺失或为空直接返回空结果；
- metadata filter 只能在该集合内进一步缩小；
- 父子块、TOC 和其他扩展读取不得扩大集合；
- `use_kg` 等没有文档范围参数的全库扩展在 `restrict` 模式下关闭；
- 不能省略过滤参数后查询全库。


## 6. EAM 用户 Memory

当前未提交代码中的 Memory 实现继续作为唯一 Gateway Memory 入口：

- `memory_subject.py` 从认证 `UserPrincipal` 生成 `eam:{tenant_id}:{business_user_id}`；
- `memory_client.py` 调用 RAGFlow Memory 搜索和写入接口；
- `user_memory.py` 在问答前读取、成功后异步提交候选；
- 读取失败/超时返回空上下文，写入失败不阻塞问答。

### 读取

每次 Workflow run 由 Gateway 调用 `fetch_user_memory_text()`，再把结果以 `user_memory` 输入传给 Begin/Agent。Memory 只用于：

- 用户表达偏好；
- 历史指代辅助；
- 解释用户问题的背景。

Memory 不得：

- 扩大 G 或修改 ACL；
- 覆盖当前 EAM/文档事实；
- 成为引用来源；
- 让无证据回答变成成功回答；
- 进入 Ingestion Pipeline 的文档 Metadata 或共享索引。

当前权威业务上下文优先于历史记忆。记忆和当前事实冲突时，以当前授权证据和当前 EAM 上下文为准。

### 写回

在 Gateway 完成最终引用处理、终态判定和消息持久化后，仅对 `completed` 且最终问题/答案非空的运行调用 `schedule_memory_candidate()`。

- 写入问题和最终公开答案；
- 不写中间答案、思维过程、原始 chunks、附件正文或未替换的流片段；
- Workflow 使用独立 `chat_id` 来源标识，用户主体仍是认证主体；
- 失败、取消、无可靠证据和幂等回放不写入；
- 写入失败不改变已完成的问答结果；
- 不启用 Canvas 自带的另一套 Memory 自动写回，避免重复候选。

### 隔离验收

覆盖同用户跨会话、同租户不同用户、不同租户相同用户 ID、伪造用户字段、Workflow 版本变化、Memory 服务失败、记忆与当前事实冲突、成功写回和失败不写回。日志只记录启用状态、命中数、耗时、错误类型和调度状态，不记录记忆正文。


## 11. Agent Workflow 详细执行路径

### Begin 输入契约

Gateway 只传服务端生成的值，实际 RAGFlow API `inputs` 使用 Begin 的 typed value
形式：

```json
{
  "authorized_dataset_ids": {"type": "array", "value": ["dataset-1"]},
  "authorized_doc_ids": {"type": "array", "value": ["doc-1", "doc-2"]},
  "doc_scope_mode": {"type": "line", "value": "restrict"},
  "business_context": {"type": "object", "value": {"model": "ABC-200"}},
  "user_memory": {"type": "line", "value": ""},
  "internet_enabled": {"type": "boolean", "value": false},
  "reasoning_mode": {"type": "line", "value": "simple"},
  "workflow_version": {"type": "line", "value": "enterprise-qa-agent-v1.4"}
}
```

前端不能传 tenant、role、dataset 或 document scope；Workflow 不能修改这些输入。

### 问题分类

分类只选择执行策略，不作权限判断：

| 类别 | 例子 | 初始执行路径 |
|---|---|---|
| `DIRECT_QA` | EQ-001 的额定功率是多少？ | 一次 Restricted Retrieval → Answer |
| `RESEARCH` | 为什么经常振动报警？ | 改写问题 → 最多 4 轮检索 → Evidence Check |
| `BUSINESS_FUSION` | 比较 EQ-001 与同型号设备故障 | 后续接入受控 EAM/Asset Tool，再与 KB 合并 |
| `ATTACHMENT_QA` | 结合上传的故障照片和说明书 | Attachment Context + Restricted Retrieval |

第一版 Agent 模板先实现知识库、附件和证据回答；EAM 实时事实、同型号关系、SQL
和 MCP 在通过工具授权评审后再加。

### 受控动态范围

`G` 是 Gateway 计算的最大授权集合，`S` 是 Agent 为本轮选择的搜索集合，始终满足
`S ⊆ G`：

```text
用户问 “比较 A 和 B”
requested = {A, B}
S = requested ∩ G

用户问 “和同型号其它设备比较”
Asset Relation Tool → candidates
S = candidates ∩ G
```

会话上下文可以保存 A、B 的业务指代，但每轮的文档集合都重新从当前 ACL 和质量
投影计算。LLM 不能构造 `doc_ids` 后直接查询，也不能用一次检索结果给下一轮
扩大授权范围。

### 工具和分支

- **Knowledge Retrieval**：Hybrid Search、metadata filter、rerank 和 chunk reference；
  `doc_scope_mode=restrict` 是硬边界。
- **Asset Relation Tool**：只返回 Gateway 已授权的设备关系候选，Agent 再求交集。
- **EAM Facts Tool**：只读设备台账、维修、保养、故障和当前状态，禁止自由 SQL。
- **Attachment Context Tool**：只读取当前 conversation 已授权附件；不建持久索引，
  不跨会话复用。
- **Web Search**：`internetAllowed=false` 默认关闭；打开后最多一次，失败继续使用
  内部证据，不把网页结果伪装成内部文档引用。

回答前经过 Evidence Gate：知识 chunk、受控 EAM record 或附件观察至少有一种可靠
依据；否则返回 `no_reliable_evidence` 和固定的“当前检索结果中没有找到可靠依据”。用户
Memory 只能辅助指代和表达偏好，不能充当 EvidenceSet。

### 引用、状态和观察

Agent 只返回自己采用的 `evidence_id` 或 `[ID:n]` 候选；Gateway 再检查授权并映射：

```text
evidence_id → authorization check → externalDocumentId/version/page/bbox/sourceType
```

Agent 不生成外部 ID、页码、bbox、文件路径或下载 URL。公共终态固定为
`completed`、`no_reliable_evidence`、`failed`，并与 citations 独立持久化。SSE 保留
`run.started`、`answer.delta`、`answer.replaced`、`citation`、`answer.completed`、
`run.failed` 和取消语义；兼容解析 `reasoning.delta`，但不转发 Agent 的原始思维过程。

每个 Workflow run 记录 `runId`、Workflow version、类别、工具名、工具耗时、检索次数、
chunk 数、Evidence 数、LLM 耗时、首段时间、总延迟和终态；不记录完整 Prompt、chunk
正文、附件正文、JWT、工具 secret、原始模型输出或模型思维过程。


## 13. 第二入口验收样例

```text
Case 1  “EQ-001 厂家是什么？”
        DIRECT_QA；一次 Restricted Retrieval；不调用 EAM Tool。

Case 2  “EQ-001 为什么经常振动报警？”
        RESEARCH；必要时多轮检索；证据不足则 no_reliable_evidence。

Case 3  “和其它同型号设备比较。”
        Asset Relation candidates → candidates ∩ G → Retrieval/EAM。

Case 4  用户无 B 权限但要求比较 A/B。
        requested={A,B}、G={A,C}、S={A}；B 不进入 Retrieval。

Case 5  无可靠证据。
        返回固定无依据答案；不使用 Memory 或模型常识补全设备事实。
```

调优时固定用户身份、Workflow version、模型、Memory fixture 和样本集，同时保留
旧 Chat 的结果作为 baseline。每轮只调整一种参数，记录首段时间、总耗时、调用量、
召回数量、回答评审、引用评审和失败类型。解析评测与查询评测分开进行，避免把 PDF
解析变化误判成 Agent 策略收益。


## Console / hot-reload (runtime `workflow`)

Gateway Console **System Settings → Integrations → Gateway runtime** exposes section `workflow`
(UI label: **Agent Workflow**):

| Field | API (camelCase) | Meaning |
|---|---|---|
| Enable switch | `enabled` | Feature gate (same role as `ENTERPRISE_WORKFLOW_ENABLED`; **default false**) |
| Agent ID | `agentId` | Applied RAGFlow agent id (prod v1.2 example: `9d6f54beb0b911f1ad1c8d8c8b7d5b0e`) |
| Version | `version` | Applied canvas version string (example: `enterprise-qa-agent-v1.4`) |
| Timeout | `timeoutSeconds` | Optional HTTP timeout → `ENTERPRISE_WORKFLOW_TIMEOUT` (1–600s) |

Save persists into `gateway_runtime_settings` and **hot-reloads in-process** (same path as
`userMemory` / `retrievalScope` / `ragflowStatusWebhook`). No Gateway restart required.

Effective path: `workflow_router` / `config.workflow_*` read `config.runtime_settings()`.
Hot toggle immediately affects `/enterprise/api/v1/workflow/*` only. **Must not** change
normal Chat / v2 ask — Chat and Workflow coexist; the client picks the API / Harness tab.

- `enabled=false` → Workflow routes report `WORKFLOW_NOT_CONFIGURED`
- `enabled=true` + missing `agentId`/`version` → same `WORKFLOW_NOT_CONFIGURED` (do **not**
  silently fall back to Chat)
- Old DB rows without `workflow` → backfill from env on load
- **Do not enable Workflow in production** unless intentionally testing the second entry

Env vars remain the boot/default source when the section is first created.
