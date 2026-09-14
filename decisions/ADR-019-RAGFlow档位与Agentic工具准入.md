# ADR-019：RAGFlow 档位与 Agentic 工具准入

## 状态

已接受，2026-09-11。

## 背景

Gateway 的 reasoningMode=simple 在部分会话配置下可能被 RAGFlow 的
prompt_config.reasoning 覆盖。Agentic 研究工具的空结果回退还会遍历阶段
优先级列表，导致档位配置外的 BM25/Wiki 被调用，并把导航工具的 topic
参数传给只接受 query 的搜索工具。导航工具返回文档 ID 时也会被错误地
显示为空结果。

## 决策

1. reasoning 值统一为 0..4。显式值优先于会话默认值，0 始终进入普通
   聊天；缺失或 null 才沿用会话默认行为。非法值在 RAGFlow 入口拒绝。
2. Agentic 工具的执行集合由档位、阶段、编译产物、联网状态和上下文共同
   决定。模型调用、文本回退和自动回退使用同一集合。
3. 自动回退只保留已注册且兼容的 hybrid_search -> bm25_search，最多一次；
   回退参数按目标工具的 schema 重建，禁止复用 topic 等不兼容参数。
4. 工具结果区分证据片段、文档路由、直接答案、工具错误和正常空结果。
   文档路由成功不等于已经产生证据。
5. 外部 JWT 只能使用 simple/medium/high；本地 Console session 保留五档。
   权限判断使用服务端认证来源，不使用请求体中的 caller 或 sourceSystem。
6. Gateway 仍负责授权文档集合、ACL 和 doc_scope=restrict；本 ADR 不改变
   Gateway 的设备授权边界，也不改变 Hybrid 内部的词法检索。

## 后果

simple、medium、high 的执行边界可通过请求档位和工具轨迹复核。High 不会
因为空结果自动进入独立 BM25/Wiki；Ultra 只有在当前阶段和工具集合都允许
时才可使用 BM25。无证据、工具错误和正常完成继续由现有业务状态契约区分。

本 ADR 不调整 Agentic 轮数、并发数、模型注册或 30 服务器部署流程。后续
性能调整必须基于本方案修复后的阶段诊断数据单独评估。
