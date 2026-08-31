# ADR-017 RAG 诊断旁路

- 状态：Accepted
- 日期：2026-08-31

## 背景

现有回答链路能返回答案、引用和业务状态，但管理员无法用同一个 `runId` 判断问题发生在
文档范围、候选排序、Context 裁剪还是模型调用。完整 Prompt、Chunk 正文和模型原始响应又
不能进入日志或管理页面。

## 决策

1. 复用 Gateway 已有 `runId`，通过 `X-Request-ID` 传给 RAGFlow，不新增追踪标识。
2. RAGFlow 仅定义一个内部 `RagDiagnosticsSink` 接口；默认 Noop，显式开启后使用请求级有界内存实现。诊断错误全部隔离。
3. 仅采集查询文本、文档与 Chunk ID、排序分数、Context 取舍、模型标识、耗时、TTFT、token 数和结果；不保存 Prompt、Chunk 正文、工具载荷或原始模型响应，也不计算内容哈希。
4. Gateway 将诊断写入现有 `ext_v2_message_run.result_json["_diagnostics"]`；不新增表、索引或迁移。公共 JSON、SSE 与回放继续过滤私有字段。
5. 管理员通过租户隔离的内部接口和只读 Console 页按 `runId` 查看诊断。功能由 `ENTERPRISE_RAG_DIAGNOSTICS_ENABLED` 控制，默认关闭。
6. 诊断不得参与引用生成、回答内容、流式输出或 `completed` / `no_reliable_evidence` / `failed` 判定。

## 边界与后果

- 不接入 Langfuse、OpenTelemetry、Prometheus 或新的依赖。
- 单条事件、集合数量和总大小均有硬上限；超限只标记 `truncated=true`。
- 首期数据生命周期沿用消息运行记录，只解决单次链路定位，不提供聚合、告警或质量评分。
- 管理接口不进入外部 OpenAPI 2.9.0。

## 升级与回滚

上游变更登记为 RF-PATCH-009。回滚时移除请求入口、检索、Context 和 LLM 的诊断调用及
`rag/diagnostics.py`，Gateway 关闭开关后即停止产生新数据；历史私有 JSON 无需迁移。
