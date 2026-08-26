# ADR：生成侧注入本轮检索范围设备身份块（上游 dialog_service / Agentic formalize 最小补丁）

- 状态：Accepted
- 日期：2026-08-25（Agentic 扩展：2026-08-26）
- 决策人：Retrieval Agent

## 背景

D2 类问题（例如「GQ01250024 的合格证型号」）在 Gateway 已正确下发 `doc_ids` / `equipment_id` 且 ES 命中正确文档后，模型仍常写「无法按该编号匹配 / 正文未找到该设备号」，同时又从 Content 列出型号。根因是生成上下文里本轮设备身份不够硬：OCR 正文往往不含 DeviceCode，仅靠企业 Prompt 与 `document_metadata` 行不够稳定。

此前否决了把设备号写入检索 query（污染 Dense/BM25）以及把身份前缀叠进 `_ragflow_question` 的方案。

简单挡 `async_chat` 已在 `kb_prompt` 后注入身份块；有推理挡位走 `rag_agent` → Agentic → `formalize_answer`，原先**不经过**该注入，且成答前不 enrich `document_metadata`，导致 D2 在 Agentic 路径仍拒认台账号。

## 约束

- 优先企业外围；确需上游改动时必须最小补丁、可单独回放/删除。
- 不改检索 query、不改 C3 点名换绑口径。
- 不得把整句用户问题当作设备标识写入身份块。
- Gateway 可传可选 `scope_identifiers`（与本轮 equipment / fixed_asset 同值）；缺省时回退 `allowed_identifiers`。
- 不改 `FINAL_ANSWER_SYSTEM` 长文案；身份说明写入 Evidence 段内即可。
- 不动 planner/orchestrator 中间步的 `kb_prompt`（只修最终成答）。

## 备选方案

1. **仅升 Prompt v11**：已证明对 D2 不够。否决为唯一手段。
2. **在 `_ragflow_question` 加身份前缀**：污染检索。否决。
3. **检索完成后、生成前**在 `knowledges` / Evidence 最前插入固定中文身份块。采纳（simple + Agentic 共用）。

## 决策

### 公共 helper

| 项 | 值 |
|---|---|
| 文件 | `ragflow/api/utils/scope_identity_prompt.py` |
| 函数 | `_filter_scope_device_identifiers` / `_build_scope_identity_knowledge_block` / `_prepend_scope_identity_knowledge` |
| 过滤 | 仅设备风格 token（字母 + 数字或 `-_.`、无空白、限长）；去重；通常 1～2 个 |

`dialog_service.async_chat` 与 Agentic `formalize_answer` 均从此模块 import，避免两套口径与 `dialog_service` ↔ `advanced_rag` 循环导入。

### 上游补丁 — simple 路径

| 项 | 值 |
|---|---|
| 文件 | `ragflow/api/db/services/dialog_service.py` |
| 函数 | `async_chat` |
| 插入点 | `knowledges = kb_prompt(...)` **之后**、`empty_response` / prompt fit **之前** |
| 标识来源 | 优先 `kwargs.scope_identifiers`；否则取 `allowed_identifiers` 在 append `last_user` **之前**的副本 |
| 不改 | `async_chat_solo`（无 knowledge 路径）；检索 query；grounding 仍可把 `last_user` append 进 `allowed_identifiers` |

### 上游补丁 — Agentic 路径

| 项 | 值 |
|---|---|
| 文件 | `ragflow/api/db/services/dialog_service.py`（`rag_agent`）、`ragflow/rag/advanced_rag/agentic_rag.py`（`RAGTools`）、`ragflow/rag/advanced_rag/agentic_rag_graph.py`（`formalize_answer` / `_build_formalize_evidence_text`） |
| 透传 | `rag_agent` 将 `scope_identifiers`（或回退 `allowed_identifiers`）传入 `RAGTools`；**不**把整句 user question append 进列表 |
| 成答前 | `enforce_doc_scope` 后，若 `tools.scope_identifiers` 非空：`enrich_chunks_with_document_metadata(..., {"equipment_id","fixed_asset_no"})` → `kb_prompt` → prepend 身份块 → `"\n".join(blocks)` 写入 Evidence |
| 不改 | `FINAL_ANSWER_SYSTEM`；Gateway；中间步 kb_prompt |

身份块大意：本轮检索范围对应这些设备标识；下列资料归属以 `document_metadata.equipment_id` / `fixed_asset_no` 为准；禁止「无法按该编号匹配 / 正文未找到该设备号」；型号/出厂编号等事实直接依据 Content。

### 上游补丁 — Agentic high：scoped keyword narrow 忽略身份 token

| 项 | 值 |
|---|---|
| 文件 | `ragflow/rag/advanced_rag/harness/tools/search.py` |
| 函数 | `_narrow_by_keywords`；由 `hybrid_search` / `vector_search` / `bm25_search` 调用 |
| 行为 | `ignore_tokens=tools.scope_identifiers`：台账号等身份词不当正文必现；`keep_unmatched_when_scoped=bool(doc_scope)`：有权威 doc_scope 且内容词全不命中时保留原 chunks，禁止清空整包 |
| 不改 | navigation / exploration / inspector 的 narrow 调用；开放域（无 doc_scope）drop 逻辑与上游一致 |
| 冲突点 | 上游改写 `_narrow_by_keywords` 签名或三档 search 的 narrow 接线；`scope_identifiers` 未透传到 `RAGTools` 时 ignore 失效 |

三档（low/medium/high）共用 `hybrid_search` → `_narrow_by_keywords`；high agent 更易把 DeviceCode 塞进 keywords，故验收重点为 high D2 Kept≥1。

### 同包加固

`ragflow/api/utils/reference_metadata_utils.py` 的 `enrich_chunks_with_document_metadata`：`doc_id` 缺失时回退 `document_id`，避免 enrichment 静默跳过导致 knowledge 无 metadata 行。

## 预期冲突点（升级重放）

- 上游 `async_chat` 在 `kb_prompt` 之后重构 knowledge 组装或重命名 `knowledges`。
- Agentic `formalize_answer` 重构 Evidence 组装（`kb_prompt` 直塞 f-string / 改节点名）。
- `RAGTools.__init__` 签名变更导致 `scope_identifiers` 透传丢失。
- `allowed_identifiers` / 新 body 字段在 `/api/v1/chat/completions` → `**req` → `async_chat` / `rag_agent` 的透传路径被收窄。
- `kb_prompt` 输出结构变更导致 prepend 后的拼接格式变化。
- `enrich_chunks_with_document_metadata` 的 chunk 主键字段命名变更。
- `_narrow_by_keywords` / `hybrid_search`（及 vector/bm25）narrow 接线被上游改写，导致 `ignore_tokens` / `keep_unmatched_when_scoped` 丢失。

## 正面影响

- D2：范围正确时 simple / Agentic 均不再因正文无 DeviceCode 拒匹配，可直接答合格证型号。
- 检索打分仍只对用户原问题。

## 负面影响和风险

- 身份块过长：限制去重后少量 ID。
- 同设备多附件混淆（洗脱机+空压机）不在本决策范围内。
- 仅靠 Prompt + 身份块，模型仍可能偶发违例。
- Agentic 成答前多一次 DocMetadata 查询；有 scope 时 chunks 通常很少。

## 验证方式

- `pytest ragflow/test/unit_test/api/db/services/test_dialog_service_scope_identity.py`
- `pytest ragflow/test/unit_test/api/utils/test_scope_identity_prompt.py`
- `pytest ragflow/test/unit_test/rag/advanced_rag/test_agentic_formalize_scope_identity.py`
- `pytest ragflow/test/unit_test/api/utils/test_reference_metadata_utils.py`
- `pytest ragflow/test/unit_test/rag/advanced_rag/test_narrow_by_keywords_scope.py --noconftest`

## 回滚方式

- 删除 `async_chat` 中 `_prepend_scope_identity_knowledge` 调用；删除 Agentic `_build_formalize_evidence_text` 中的 enrich / prepend；`RAGTools` 去掉 `scope_identifiers`。
- 删除或停用 `scope_identity_prompt.py`。
- 恢复 enrichment 仅读 `doc_id`。
- Gateway 可停止发送 `scope_identifiers`（可选字段）。
- 恢复 `_narrow_by_keywords` 为无 `ignore_tokens` / `keep_unmatched_when_scoped` 的上游行为；三档 search 去掉 scope 接线。

## 对上游升级的影响

本 ADR 记录可独立 cherry-pick / 删除的核心补丁（helper 模块 + simple 注入 + Agentic formalize）。升级 RAGFlow 时按上表冲突点逐项重放。
