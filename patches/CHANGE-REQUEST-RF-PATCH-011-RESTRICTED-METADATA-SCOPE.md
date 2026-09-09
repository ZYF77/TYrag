# CHANGE REQUEST：RF-PATCH-011 Gateway 授权范围与 Metadata Restrict

## 原因

Gateway 需要把授权文档集合 G 作为每轮硬上限，同时允许 RAGFlow 在 G 内根据重写后的
问题执行 metadata 条件过滤。官方 metadata helper 的 extend 语义需要保留，企业路径
必须通过显式 doc_scope_mode=restrict 启用，避免影响普通 chat、search、mindmap 和
其他 Agentic 调用方。

## 最小上游修改

- ragflow/common/metadata_utils.py：增加 opt-in restrict 分支，固定 None、[]、["-999"]
  的空集合语义；内存 fallback 和 pushdown 结果都限制在传入文档集合。
- ragflow/common/metadata_es_filter.py：给 ES 查询增加独立的文档 ID terms 条件。
- ragflow/api/db/services/doc_metadata_service.py：metadata loader、ES 和 Infinity
  pushdown 接受可选文档 ID集合。
- ragflow/api/db/services/dialog_service.py：普通聊天在问题重写、语言处理后执行
  metadata；Agentic 入口将 metadata 推迟到 RAGTools.formalize 之后；显式空范围不进入
  向量、SQL、知识图谱或 Web fallback；保留最终硬文档过滤。
- ragflow/rag/advanced_rag/agentic_rag.py：保留每轮 doc_scope，增加严格空范围和软业务
  上下文处理。
- ragflow/rag/advanced_rag/agentic_rag_graph.py：让 Agentic 身份提示使用授权范围和
  文档 metadata 事实。
- ragflow/rag/advanced_rag/harness/pipeline.py：工具范围继承区分 None 与显式空列表。
- ragflow/rag/advanced_rag/harness/tools/search.py、ragflow/rag/advanced_rag/harness/tools/navigation.py：
  防止 Agentic 检索工具在 restrict 空范围时回退到全库或全数据集。
- ragflow/rag/advanced_rag/harness/tools/registry.py：说明 Gateway restrict 模式下空
  doc_scope 的 fail-closed 语义。
- ragflow/rag/prompts/generator.py、ragflow/api/utils/scope_identity_prompt.py：问题
  重写和身份提示明确软上下文不属于授权或证据。

Gateway 侧同步增加策略开关、v2 会话业务字段、schema v7 以及运行快照，并在 HTTP
客户端中透传 doc_scope_mode 和已确认的六个软上下文字段。Evidence Contract、Grounding
和最终 doc_ids 投影保持不变。

## 契约与安全

- ENTERPRISE_RETRIEVAL_SCOPE_POLICY 缺失或非法时回退 legacy_device。
- authorized_context 请求的 doc_ids 始终是 Gateway 计算的 G；RAGFlow 不能扩域。
- business_context 只包含 equipment_id、fixed_asset_no、fault_code、model、
  equipment_type、manufacturer，仅用于问题理解和提示词说明，不用于授权或证据判定。
  不会发送原始问题作为业务上下文。
- 业务条件零命中必须返回空证据，禁止回退全库。

## 测试

- ragflow/test/unit_test/common/test_restricted_metadata_scope.py
- ragflow/test/unit_test/api/utils/test_scope_identity_prompt.py
- ragflow/test/unit_test/rag/advanced_rag/test_gating_and_doc_scope.py
- ragflow/test/unit_test/api/db/services/test_dialog_service_scope_identity.py
- enterprise/tests/test_ragflow_client.py
- enterprise/tests/test_agentic_scope_and_planner.py
- enterprise/tests/test_gateway_db.py
- enterprise/tests/test_v2_conversation_contract.py

## 升级与回滚

构建包含 RF-PATCH-011 的新 RAGFlow 镜像，并先以 legacy_device 运行。Gateway schema 升级
到 v7 后，在测试租户启用 authorized_context，验证空集、跨租户和串设备负例。这次变更
不包含服务器 30 部署或推送。回滚使用旧 RAGFlow 镜像并将开关恢复为 legacy_device；
新增数据库列可以保留。
