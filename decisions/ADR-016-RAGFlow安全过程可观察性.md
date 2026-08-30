# ADR-016 RAGFlow 安全过程可观察性

- 状态：Accepted
- 日期：2026-08-31

## 背景

`grounding_version=1` 为避免泄露企业 Prompt、问题原文和知识正文，原先把 completion 的
`prompt` 置空，并将 Agentic 进度日志裁剪为阶段标签。RAGFlow 前端因此隐藏 Prompt
小灯泡；Agentic 流结束时 `structure_answer` 又用最终答案覆盖已流出的 `<think>`，刷新会话
后通常只剩模型产生的 `Running the rag tool...`。

## 决策

1. Grounding completion 的 `prompt` 字段改为固定的安全说明，不包含问题、Prompt、文档正文、工具参数或工具结果；继续复用既有 RAGFlow Prompt 对话框。
2. 阶段日志继续只允许静态阶段标签，并为已知阶段附加固定描述；未知阶段只保留标签。
3. Agentic 流式 final 在 RAGFlow 内保存去重后的脱敏阶段摘要，放入一个 `<think>` 区块；最多保存 64 个阶段，模型自身的 `<think>` 与阶段摘要合并。
4. 会话结构化时只保留固定的 grounding 安全摘要 `prompt`，保证刷新或重新进入会话仍显示小灯泡；普通链路的完整 Prompt 不落历史；不新增数据库字段或公共 API。
5. Gateway 仍按现有 `reasoning.delta` / `reasoning` 处理，不发送或持久化未脱敏日志。

## 后果

- RAGFlow 新产生的 grounding 消息可打开小灯泡，并能查看安全说明；Agentic 消息可查看检索和编排阶段。
- 旧会话不会自动补齐摘要；不需要数据迁移。
- `prompt` 在 grounding 下不再是空值，依赖“空值表示已脱敏”的外部代码需改用内容策略判断。
- 不恢复原始 Chain-of-Thought、检索词、知识正文、工具入参或返回值。

## 升级与回滚

上游变更集中在 `think_log.py`、`dialog_service.py` 和 `conversation_service.py`，通过独立
测试验证。升级到新 RAGFlow 时逐项重放；回滚时同时移除安全摘要、阶段聚合和会话 prompt
持久化逻辑，不涉及迁移。
