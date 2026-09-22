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

