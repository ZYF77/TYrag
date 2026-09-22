# New2 v2.4

version: **New2 v2.4**

Base: `New2_v2.3_evidence_scope.json`

## Changes
1. **PlanLite** (`Agent:Plan`): required fields shrink to route / resolved_question / scope_mode / target_equipment_ids / focused_query / broad_query / clarification_question / boundary_response; `scope_source` optional; `max_tokens` 900.
2. **AnswerResearch** (`Agent:AnswerResearch`): merges former Draft + Research into one Agent after Initial Retrieval; enough evidence answers without tools; otherwise tool supplement with `max_rounds≤2`.
3. **Retrieval**: Initial `Retrieval:Target/Scope` top_n=8; Research tools TargetEvidence/AuthorizedEvidence/KeywordEvidence top_n=4; prompt forbids fixed Target→Authorized→Keyword pipeline.
4. **B-level speech**: with related cite, must explain related material + why it cannot migrate to the target object; forbid whole-sentence “未找到可靠依据/无法回答” while cites remain.
5. Graph: removed independent Draft serial path and Coverage→Research second full-answer LLM.

## Non-goals
- No RF recreate / Gateway hard-narrow G
- No Evidence Policy A1/A2/B relaxation

## Validate
```bash
python3 artifacts/workflows/check_new2_v24.py
```

## Hotfix (Sim KeyError)
- `Agent:AnswerResearch` graph node type must be `agentNode` (was wrongly `ragNode` after v2.4 transform), otherwise Seed succeeds then KeyError before AnswerResearch runs.

## Hotfix2 (Sim still KeyError after agentNode)
- Rebuild all graph edges with `sourceHandle`/`targetHandle` (v2.3 style). Transform had stripped them; Seed could finish while AnswerResearch never started.

## Hotfix2b (Probe)
- Runtime KeyError is exactly `name`: `canvas.get_component_name` reads `graph.nodes[Agent:AnswerResearch].data["name"]`.
- Bad PUT had `type=ragNode` and data with only label+form (no name).
- Fix requires both `type=agentNode` and non-empty `data.name`.

## Hotfix3 (Sim S1/S2 答效)
- S1：Initial 已有 A1/A2（如厂家名）时 AnswerResearch 必须 `completed` + Direct，**禁止调工具** / 禁止改 NoEvidence。
- S2：B 级话术「相关+不能迁移」时 `evidence_ids` 必须非空。
- S3：真无证据路径保持不变。

