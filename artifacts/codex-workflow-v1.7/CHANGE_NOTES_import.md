# CHANGE NOTES — Enterprise QA Agent v1.7.1 (semi_auto equipment_id)

Date: 2026-09-16 (Asia/Shanghai)
Scope: Builder harden of v1.7 Chat Baseline. No CloudAgent. No compose. No Gateway ACL.
Do **not** commit/push from this Builder pass.

## Version

- Display / Agent title: `TYrag Enterprise QA Agent v1.7.1`
- `workflow_version` / Console: `enterprise-qa-agent-v1.7.1`
- Importable artifacts:
  - `enterprise/workflows/enterprise_qa_agent_v1.template.json` (canonical template)
  - `artifacts/codex-workflow-v1.7/enterprise_qa_agent_v1.7.normalized.json`
  - `artifacts/codex-workflow-v1.7/enterprise_qa_agent_v1.7.1.normalized.json` (same bytes, versioned name)

## What landed (v1.7 → v1.7.1)

1. **JSON-harden `FocusedEvidence.meta_data_filter`** (design-time ids unresolved → hand-write in JSON):
   ```json
   {
     "method": "semi_auto",
     "logic": "and",
     "manual": [],
     "semi_auto": [{"key": "equipment_id", "op": "="}]
   }
   ```
   Aligns with RF `MetadataFilterSchema` (string or `{key,op}`). LLM extracts `equipment_id`
   from the **query** text; if the key is absent from KB metas, the whole filter is skipped
   (RF behavior). Not full `auto`. Not manual empty-value bypass.

2. **QueryRefiner** sys_prompt: when `business_context` / user question already contains a
   clear `equipment_id` / 设备号, **must** pin it into the refined query so semi_auto can
   extract it; if neither has an equipment id, **do not invent** one.

3. **Scope bindings unchanged and enforced**:
   - `dataset_ids` **only** `["begin@authorized_dataset_ids"]`
   - `doc_scope_ids` **only** `["begin@authorized_doc_ids"]`
   - `doc_scope_mode=restrict`
   - Forbid display-name space binding, mixing other `begin@` / `sys.*`, hardcoding real KB UUIDs.

4. Static validators / tests updated: assert exact dataset_ids binding; assert semi_auto
   form (no longer require empty `{}`); assert QueryRefiner mentions equipment_id;
   version pin `enterprise-qa-agent-v1.7.1`.

## Import / UI caveats (read before RF import)

- **Do not pick Knowledge Base by canvas display name** on Retrieval.
  UI “Knowledge Base” picker writes display labels / unresolved placeholders and has caused
  `101 Invalid UUID` and `102` with literal `begin@authorized dataset ids` (spaces / wrong key).
  The template key must remain exactly `authorized_dataset_ids` via binding
  `begin@authorized_dataset_ids`.
- **After import, do not UI-overwrite `dataset_ids` / `doc_scope_ids` / `meta_data_filter`.**
  Re-opening the Retrieval node and clicking KB chips or clearing metadata will regress the
  JSON harden. Prefer re-import of the normalized JSON if the canvas drifts.
- Gateway G∩F hard-narrow, manual empty-value bypass, full auto, and ACL changes remain
  **forbidden** in this pass.

## Validators / tests

```bash
python artifacts/codex-workflow-v1.7/validate_workflow.py
python artifacts/codex-workflow-v1.7/validate_workflow.py enterprise/workflows/enterprise_qa_agent_v1.template.json
python enterprise/scripts/validate_workflow_artifacts.py
pytest enterprise/tests/test_workflow_artifacts.py -q
```

## Explicitly NOT done

- No CloudAgent / compose / Gateway ACL / G∩F hard-narrow.
- No commit/push.
- No RF import / Console hot-set (next: QA → import).
- Citation-id Gateway patch still deferred to Deploy.

## Residual risks

- semi_auto depends on QueryRefiner actually emitting equipment_id when present; if the
  refined query omits it, metadata filter may no-op or miss.
- If KB documents lack `equipment_id` in metas, RF skips the filter (whole filter skipped
  when selected keys are absent from metas) → falls back to G-only restrict scope.
- UI edit after import can silently replace `begin@authorized_dataset_ids` with display names.
