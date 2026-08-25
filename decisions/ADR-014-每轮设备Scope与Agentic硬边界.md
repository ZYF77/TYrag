# ADR-014 每轮设备 Scope 与 Agentic 硬边界

- 状态：Accepted
- 日期：2026-08-25

## 决策

1. Conversation 设备字段表示当前活动实体，不再永久锁定检索范围。每个 message run 持久化 `entity_scope_json` 和 `allowed_doc_ids_json`。
2. Gateway 先取得同租户可用文档，再按本轮确定性设备实体求交集；未知明确实体 fail closed，模糊描述不由 LLM 猜测。
3. 联调阶段文档角色 ACL 采用 `test-tenant-open-1`：保留有效用户、tenant 和 active 检查，暂时忽略 department、group 和 security level。接口 capability、current version、质量门和设备 Scope 不放宽。
4. RAGFlow 将 Gateway 的 `doc_ids` 视为 immutable initial scope。Tool、compiled/wiki evidence、最终 Prompt 和 reference 只能缩小该范围。
5. compiled/wiki chunk 仅在 `source_doc_ids` 唯一归属一个允许文档时保留；无法归属或多来源直接丢弃。合法 web evidence 仅在本轮启用联网时例外保留。
6. Gateway 的 `RAGFLOW_SCOPE_VIOLATION` 校验继续作为最后一道 fail-closed 防线。

## 上游最小补丁

- `rag/advanced_rag/agentic_rag.py`：统一 scope 过滤函数。
- `rag/advanced_rag/agentic_rag_graph.py` 与 harness pipeline：Tool merge 和 Prompt 前调用统一过滤。
- `api/db/services/dialog_service.py`：reference 生成前再次过滤。
- planner 对非对象 JSON 和非数组 claims 降级为 direct plan。

这些修改不得与上游目录重排或无关重构混合。升级 RAGFlow 时逐项重放；若上游已提供等价 immutable scope，可删除对应补丁。

## 后果

- 同一 EAM Conversation 可以切换和比较设备，且每次幂等回放保持原 Scope。
- High/Ultra 的自主规划保留，但不能扩大企业文档范围。
- 测试阶段同租户用户可见全部可用文档；正式角色 ACL 必须在投产前以新 ADR 恢复。
