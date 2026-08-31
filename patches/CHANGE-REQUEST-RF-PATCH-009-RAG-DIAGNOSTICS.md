# CHANGE REQUEST：RF-PATCH-009 RAG 请求级诊断旁路

## 原因

Gateway 需要在不暴露正文、不改变问答行为的前提下，用现有 `runId` 定位 RAG 请求在范围、
检索、Context 或模型阶段的失败。RAGFlow 公共接口没有提供这组请求级安全元数据。

## 最小上游修改

- `ragflow/rag/diagnostics.py`
  - 唯一接口 `RagDiagnosticsSink`，Noop 与请求级有界内存实现。
- `ragflow/api/apps/restful_apis/chat_api.py`
  - 读取私有开关与 `X-Request-ID`，仅在最终数据中附加 `_diagnostics`。
- `ragflow/rag/nlp/search.py`
  - 记录候选 ID、文档 ID、排序、分数、阈值和入选结果，不改排序公式。
- `ragflow/api/db/services/dialog_service.py`
  - 记录元数据过滤后的文档范围和最终 Context Chunk ID。
- `ragflow/rag/advanced_rag/agentic_rag_graph.py`
  - 记录 Agentic 最终证据 Context 的 Chunk ID 取舍。
- `ragflow/api/db/services/llm_service.py`
  - 在统一 chat 调用点记录模型标识、耗时、TTFT、token 与结果。

## 安全与兼容性

- 私有诊断仅在 `enterprise_diagnostics=true` 且存在 `X-Request-ID` 时产生。
- 禁止 Prompt、Chunk 正文、模型原始响应、工具参数与工具结果；所有集合和总大小有上限。
- Sink 异常被吞掉，不影响回答、引用、状态、检索与流式输出。
- 不改数据库模型、迁移、公共 OpenAPI、文档引擎或全局状态枚举。

## 测试

- `ragflow/test/unit_test/rag/test_diagnostics.py`
- Gateway RAGFlow client 契约与 v2 私有字段回归测试。
- 最终门禁为本机真实 HTTP JSON、Agentic high SSE 与失败/中断链路 E2E。

## 回滚

移除上述诊断调用与模块即可；Gateway 将功能开关关闭后不会再请求或持久化诊断，已有消息
运行记录保持可读，无数据迁移。
