# ADR：RAG 诊断同时记录阶段耗时与累计时间

## 状态

Accepted

## 背景

RAG 诊断原先只有事件写入时的 `atMs`。该字段表示从 trace 开始到事件记录时的累计时间，不能直接回答某个阶段自身耗时多少；而且 Gateway 在请求结束时合并 RAGFlow trace，会让上游事件看起来集中在同一时刻。

## 决策

- 保留 `atMs`，明确其语义为 `cumulative_from_trace_start`。
- 为有计时的事件增加顶层 `durationMs`，语义为当前事件/阶段自身耗时；旧事件没有该字段时由 UI 显示“未提供”。
- RAGFlow 为元数据过滤、多轮问题改写、跨语言改写、关键词分析、Embedding、候选检索、Rerank、引用元数据补充、Context 组装和最终生成记录阶段事件。
- LLM 事件携带当前逻辑阶段名；不记录 Prompt、知识正文、原始回答或工具参数。
- Gateway 合并上游事件时保留 RAGFlow 的 `sourceAtMs`，并按上游 trace 总耗时估算其 Gateway 时间轴偏移，避免把所有事件显示在请求末尾。
- 诊断接口继续保持私有、租户隔离和旧 trace 可读，不改变业务响应契约。

## 取舍与风险

阶段事件会略微增加诊断存储量，但仍受既有事件数和字节上限约束。跨进程时间轴的偏移是基于请求结束时的上游总耗时估算，精确定位依赖 `durationMs`，不把 `atMs` 当作跨服务精确时钟。

## 升级/回滚

该变更只涉及诊断埋点和 WebUI 展示。升级 RAGFlow 时需保留 `rag/diagnostics.py`、`api/db/services/llm_service.py`、`api/db/services/dialog_service.py`、`rag/nlp/search.py` 的补丁；删除这些补丁即可回退到旧的累计时间展示。
