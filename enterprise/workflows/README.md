# TYrag Workflow v1.7.1 Chat Baseline

`enterprise_qa_agent_v1.template.json` 是 canonical Agent 画布模板（canonical DSL：
`graph.nodes` / `graph.edges` + `components`），经 Agent JSON 上传导入；不要再包一层
`dsl`。`path` 必须为 `[]`。

版本：`enterprise-qa-agent-v1.7.1`（标题：TYrag Enterprise QA Agent v1.7.1）。

## 编排（v1.7.1 Chat Baseline）

对齐生产 Chat simple 检索快照，去掉 Categorize / Research / FinalGuard 分支，优先恢复
「能答则答」。v1.7.1 在 v1.7 基础上 JSON 硬化 `meta_data_filter=semi_auto/equipment_id`，
并由 QueryRefiner 把业务上下文/原问中的设备号钉进改写查询。

```text
Begin（授权 / 业务上下文 / Memory）
  → Agent:QueryRefiner（轻改写；钉住 equipment_id；不缩 G）
  → Retrieval:FocusedEvidence（Chat 对齐 + rerank + restrict + semi_auto）
  → Agent:FocusedAnswer（Chat v12 拒答句 + formalized_content）
  → Message:FinalAnswer
```

### Retrieval（Chat 对齐）

| 项 | 值 |
|---|---|
| 对话模型 | `ep-20260310093543-zl952@LLM@VolcEngine` |
| rerank | `qwen3-rerank@千问@Tongyi-Qianwen` |
| top_n / top_k | 6 / 10 |
| similarity_threshold | 0.1 |
| keywords_similarity_weight | 0.7 |
| doc_scope_mode | `restrict` + `begin@authorized_*` |
| meta_data_filter | `semi_auto` + `equipment_id`（op `=`；非 `{}`、非 full auto） |

`workflow_version` 默认：`enterprise-qa-agent-v1.7.1`。

### 为何不能在画布用显示名选知识库

Retrieval 的 Knowledge Base 选择器按**显示名**绑定，设计时 UUID 未解析时会写入非法值，
线上已出现：

- `101 Invalid UUID`
- `102` 且字面量变成 `begin@authorized dataset ids`（空格 / 错误 key）

正确写法只能是模板键 **`authorized_dataset_ids`**，通过绑定
`["begin@authorized_dataset_ids"]`。导入后**不要**在 UI 里改点 `dataset_ids` /
`doc_scope_ids` / `meta_data_filter`，否则会覆盖 JSON 硬化；画布漂移时重新导入
normalized JSON。

## 硬约束

- 全部 Retrieval：`dataset_ids=begin@authorized_dataset_ids`、
  `doc_scope_ids=begin@authorized_doc_ids`、`doc_scope_mode=restrict`；空 scope → 空检索。
- 禁止：画布显示名绑 KB、硬编码真实 KB UUID、混入其它 `begin@` / `sys.*`、Gateway G∩F
  硬收窄、manual 空值旁路、full auto、改 ACL。
- 默认 chat 模型：VolcEngine 豆包（非 DeepSeek）；rerank 与生产 Chat 相同。
- 未注册 Web Search；`internet_enabled` 默认 false。
- `user_memory` / `business_context` 仅辅助消歧与表达，不是设备事实证据。
- 无证拒答固定文案：`当前检索结果中没有找到可靠依据`；禁止旧句「未找到可靠依据，无法回答。」。
- Gateway `citation_id` map bug 仍在，需后续 Deploy 应用
  `artifacts/codex-workflow-v1.7/gateway-workflow-citation-id.patch`（本轮不应用）。

## 准备 / 校验

```bash
python artifacts/codex-workflow-v1.7/validate_workflow.py enterprise/workflows/enterprise_qa_agent_v1.template.json
python enterprise/scripts/validate_workflow_artifacts.py
python enterprise/scripts/prepare_workflow_agent.py --output /tmp/prepared.json
pytest enterprise/tests/test_workflow_artifacts.py -q
```

不要自动导入 8080；由 Builder/Leader 决定何时 RF import + Console hot-set。

`eam_ingestion_pipeline_v1.json` 是独立 Data Flow 模板（PDF/DeepDoc + JSON/JSONL + TokenChunker）。
