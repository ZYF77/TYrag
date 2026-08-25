# RF26-WP04 Chat、Session、SSE 与业务历史

## 目标

在不改变 EAM Query v2.9.0 URL、字段、状态或 SSE 事件的前提下，使用 RAGFlow v0.26.4 官方 session/completion 作为执行引擎；Gateway 继续是业务 conversation、run、幂等与历史的事实源。

## 范围与非范围

范围：chat/session 创建映射、JSON/SSE completion、`clientMessageId` 幂等、run 状态、历史回放、业务状态和 citation snapshot 的边界。非范围：删除 legacy v1/demo API、改 SSE 事件名/次序、以 citations 推导状态、引入新的 EAM 协议字段、把 Gateway 业务状态迁到 RAGFlow 官方库。

## 真实调用链

`POST /enterprise/api/v2/conversations` → Gateway JWT/tenant/user 与 contextVersion → `POST /api/v1/chats/{chat_id}/sessions` → Gateway 保存 business conversation 和 `ragflow_session_id`。`POST .../messages`（JSON 或 `Accept: text/event-stream`）→ ownership/ACL/quality/`clientMessageId` → `POST /api/v1/chat/completions`（chatId、sessionId、question、stream）→ Gateway 持久 user/run/assistant 事实，转换为 `run.started`、`reasoning.delta`、`answer.delta`、可选 `answer.replaced`、`citation`、`answer.completed|run.failed` → EAM。

已完成重放只返回合并后的持久内容；pending 重放返回同 runId/202，不能创建第二次 RAGFlow completion 或伪造 SSE。

## 接口与责任归属

RAGFlow：chat/session 与 completion transport。Gateway Retrieval/Query：JWT/ownership、`clientMessageId` 幂等、业务 run 状态、SSE 适配、历史与状态。EAM：保持 v2.9.0 请求/响应消费。`completed`、`no_reliable_evidence`、`failed` 由显式运行结果决定，citations 独立持久并原样回放。

## 精确实施任务

1. 用官方 session endpoint 创建/取得 session，隔离 tenant 的 chat 配置与 business conversation；不将 EAM 用户直接映射成 RAGFlow 管理账户。
2. 将 JSON 与 SSE 两个入口汇到同一 run/idempotency 状态机，确保 stream 与非 stream 发送同一 retrieval/completion 输入。
3. 对固定 `(conversationId,clientMessageId)` 实施同 payload 回放、异 payload 409、running 202/稳定 runId；租约中断仅标 `RUN_INTERRUPTED`，不自动重跑。
4. 严格保持 v2.9.0 事件顺序和 `answer.replaced` 回放规则；上游 chunk/reference 不可直接透传为 EAM wire。
5. 持久业务历史后再返回 terminal event；历史展示保留已有状态和 citation snapshot，不改判。
6. 测试 JSON/SSE 成功、流中断、answer.replaced、重复 clientMessageId、archived conversation、跨用户会话访问。

## 依赖、验收与回滚

依赖：WP00 API 基线、WP03 前置 ACL/retrieval。验收：同一消息在 JSON/SSE 中产生一致 terminal 状态和引用；历史重放无第二次模型调用；外部 v2.9.0 contract 不变。回滚：恢复上一 Gateway run/session adapter；保留已持久 business history，不批量转换状态或删除 session。

## Agent 目录所有权

Retrieval Agent：`enterprise/gateway/query`、会话/业务查询适配。Lead：主 OpenAPI/共享路由收口。Frontend Agent 只消费已有事件，不能借本包改外部 contract。
