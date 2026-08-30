# CHANGE REQUEST：RF-PATCH-008 RAGFlow 安全过程可观察性

## 原因

Grounding 安全定制正确隐藏了完整 Prompt 和进度日志正文，但同时把 RAGFlow Prompt 小灯泡
和 Agentic 阶段历史清掉了。用户只能看到模型的通用 `Running the rag tool...`，无法确认检索
流程是否运行。

## 最小上游修改

- `ragflow/rag/advanced_rag/think_log.py`
  - 新增静态阶段描述函数；未知标签仍只保留标签。
  - grounding 外送使用静态描述，不复制日志正文。
- `ragflow/api/db/services/dialog_service.py`
  - grounding `prompt` 返回固定安全说明。
  - Agentic 流收集最多 64 个去重的脱敏阶段，并合并到最终 `<think>`。
- `ragflow/api/db/services/conversation_service.py`
  - 结构化会话消息只保留固定 grounding 安全摘要 `prompt`，让已有小灯泡在历史会话中可见；普通链路的完整 Prompt 不落历史。

不改 Gateway、ACL、检索、引用、消息状态、数据库模型或公共 API。

## 安全与兼容性

- 阶段描述为固定字符串；不会回显问题、Prompt、知识正文、工具参数或结果。
- 最终 `<think>` 只包含安全阶段和模型原有可见思考；Gateway 现有 reasoning 解析继续生效。
- grounding 消息的 `prompt` 从空值变为说明文本；需要用 `prompt` 是否为空判定策略的调用方应迁移到明确的策略字段（本仓库无此调用方）。

## 测试

- `ragflow/test/unit_test/rag/advanced_rag/test_think_log.py`
- `ragflow/test/unit_test/api/db/services/test_dialog_service_grounding.py`
  - 安全摘要、小灯泡字段、阶段聚合、去重和敏感内容负向断言。
- Gateway reasoning 映射回归测试保持通过。

## 回滚

删除本 patch 的静态摘要、阶段聚合和会话 prompt 保留逻辑即可，无数据迁移；Gateway 无需变更。
