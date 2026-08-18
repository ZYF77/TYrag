# ADR：设备身份以 Gateway canonical metadata 为准，OCR 正文不是归属证据

- 状态：Accepted
- 日期：2026-08-16
- 决策人：Retrieval + File Sync

## 背景

EAM 投喂时 Gateway 已按 `equipment_id` / `fixed_asset_no` 建立文档映射，并在问询时用 `doc_ids` 做设备范围过滤。但文档 OCR 正文常不包含设备号（例如发票）。企业 Chat 使用 RAGFlow 默认 Prompt（「irrelevant → not found in the dataset」）时，模型会因正文未再写设备号而否定文档归属，即使用户问的是「该设备有哪些资料」。

曾考虑把设备号写入 chunk 正文与 `important_kwd`。这会绕过「归属应由文档 metadata 证明」的分层，并污染检索打分。

## 约束

- 优先企业外围适配，不改 RAGFlow 上游默认 Prompt，不改 task_executor / chunk_service。
- RAGFlow `/api/v1/chat/completions` 的 `question` 同时用于检索与生成，公开 API 无法拆成两条 query。
- 旧文档不自动回填；仅新投喂 / 新版本写入身份 metadata。
- 本工作包只解决文档归属身份，不宣称解决编造工单、料号、统计数字或联网检索。

## 备选方案

1. **Chunk 正文 / important_kwd 注入设备号**：能「治好」测试，但绕过归属判断错误，污染 Dense/BM25。否决。
2. **在 `_ragflow_question` 叠设备身份前缀**：未绑定或已绑定时把身份说明塞进检索 query。chunk 故意不含设备号时会降低相似度。否决。
3. **Document meta_fields 存一份 + 企业 Chat Prompt 两层相关性 + reference_metadata 白名单**：采纳。

## 决策

1. Gateway 文档映射中已确认的 **canonical** `equipment_id` / `fixed_asset_no` 写入 RAGFlow Document `meta_fields`（有值才写）。它们证明「这是这台设备的文档」，**不是** OCR ground truth，也不写入 `enterprise_quality_ground_truth_json`。
2. 企业 Chat `prompt_config` 开启 `reference_metadata.include`，白名单仅含：`equipment_id`、`fixed_asset_no`、`enterprise_document_type`、`enterprise_external_document_id`。生成时以 `document_metadata` 暴露归属。
3. 企业 system Prompt 明确两层相关性：归属看 metadata；具体事实必须由 Content 支持。归属成立 ≠ 问题可答。无正文依据时须说明没有可靠依据，不得按设备归属推测。
4. `_ragflow_question` 原样转发用户问题（绑定与未绑定均不加 Gateway 身份前缀，也不拼 `GLOBAL_QUESTION_PREFIX`）。用户问题里自己写的设备号保留。未绑定场景仍用 `_with_equipment_hint` 在回答末尾提示补充设备号。
5. `_ensure_chat` 仅在首次创建 `enterprise-formal-{tenant}` 时写入上述 `prompt_config`。创建之后 RAGFlow 为 prompt 权威源；Gateway 不再因标记缺失或 metadata 漂移 PATCH `prompt_config`，已有助手只在 ACL `dataset_ids` 缺成员时补数据集。

## 本工作包明确不做

以下不属于本包，**不表示永久否决**，可另开工作包：

- Inquiry Grounding Guard
- Identifier Guard
- Numeric Grounding
- Web Search

无正文证据时的拒答，本包依赖 Prompt 两层相关性与现有 Gateway 空检索 → `no_reliable_evidence`。企业 Prompt v6 要求无法支撑用户当前所问事实时写出约定拒答句「当前检索结果中没有找到可靠依据」且不得标 `[ID:n]`；半支撑回答必须用本轮方括号 `[ID:n]` 引用支撑片段，禁止沿用上一轮编号。Gateway 见到拒答短语则强制 `no_reliable_evidence` 并清空 `citations`；`completed` 时除 `[ID:n]` 外也识别正文中的 `知识库ID:n` / `ID:n` 散文引用；若编号相对本轮 chunk 列表越界但本轮仅有很少检索结果（≤2），仍保留本轮支撑文档，避免多轮 ID 漂移导致参考附件为空。

## 正面影响

- 问「该设备有哪些资料」且命中发票时，模型可依据归属概括发票，而不因正文无设备号说「库里没有该设备」。
- 问「有没有漏气维修记录」且只有发票时，模型仍应说没有可靠依据，而不是用发票编维修事实；Gateway 将该负向结论标为 `无可靠依据` 且 `citations=[]`。
- 检索打分继续只对用户原问题，不被 Gateway 身份前缀污染。

## 负面影响和风险

- 旧文档 `meta_fields` 可能仍无设备号，直到新版本投喂。
- 仅靠 Prompt 约束，模型仍可能偶发误判；更强的事实接地需后续 Grounding 工作包。
- 已存在的企业 Chat 创建后不再由 Gateway 自动升级 prompt；管理员在 RAGFlow 中的修改会保留，代码里的新种子只影响新租户首次创建。助手被删后 Gateway 会按当前种子重建，手工修改丢失。

## 验证方式

- `pytest enterprise/tests/test_equipment_identity_prompt.py`
- `pytest enterprise/tests/test_file_share_v3_status.py enterprise/tests/test_v2_conversation_contract.py enterprise/tests/test_query_contract.py -q`
- 手工：新投喂后 `meta_fields` 有设备号；chunk 正文无前缀、`important_kwd` 无设备号。

## 回滚方式

- 恢复 `_external_meta_fields` 不写 `equipment_id` / `fixed_asset_no`。
- `_ensure_chat` 不再传企业 `prompt_config`；必要时手动把 Chat 改回默认 Prompt。
- `_ragflow_question` 恢复未绑定时拼接 `GLOBAL_QUESTION_PREFIX`。

## 对上游升级的影响

无核心补丁。仅使用 RAGFlow 公开 Chat `prompt_config` / `reference_metadata` 与 Document `meta_fields` API。升级时注意这两个字段的兼容性即可。
