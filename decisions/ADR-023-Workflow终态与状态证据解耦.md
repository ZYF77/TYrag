# ADR-023 Workflow 终态与状态证据解耦

- 状态：Accepted（代码已实现，固定环境验收待执行）
- 日期：2026-09-21
- 范围：企业 Gateway、企业前端；不修改 RAGFlow 上游、生产 Workflow、OpenAPI 或数据库 schema

## 问题

Agent Workflow 的 JSON 响应可能省略 `workflow_finished`，而 SSE 会先发送部分
Message。仅凭 HTTP EOF、正文非空、usage 或引用数量无法证明执行完成，也无法区分
`failed`、`no_reliable_evidence` 与安全过滤后的正文。原有 Chat/Workflow 分支还会按
状态替换正文并清空 citations，破坏消息状态、正文和证据的独立契约。

## 决策

1. Gateway 内部把 Agent JSON 请求转换为对同一 SSE 协议的消费，JSON 与公开 SSE 共用
   `WorkflowEventCollector`。只有有效 `workflow_finished` 才能确认 Workflow 终态；
   缺终态、断流和取消走 `RUN_INTERRUPTED`，畸形/不支持事件走
   `RAGFLOW_API_INCOMPATIBLE`。
2. `message_end.status` 是业务状态来源。多个 Message 取最后一个带状态的 Message；
   任意明确 `failed` 单调保持失败；显式终态与最终 Message 冲突按协议错误处理。
   `node_finished.error` 只记录执行异常，后续合法终态可以恢复；不能用正文、固定拒答
   句或 citations 推导状态。Gateway 授权范围为空时直接返回显式
   `no_reliable_evidence`，不调用不存在的上游。
3. 状态、正文、citations 分别安全处理和持久化。`completed` 可无引用；
   `no_reliable_evidence`/`failed` 可保留说明正文和授权引用。失败正文不进入成功 JSON，
   JSON 仍返回既有非 2xx；SSE 只发 `run.failed`，失败前已安全发送的正文/引用仍可见。
4. 失败消息与运行终态在现有 Gateway 数据库事务中一起保存，不引入跨请求锁、续租或
   fencing；F01/F05 的并发边界保持独立。前端明确显示“回答未完成，不可视为最终答案”，
   历史和回放按持久化状态展示。

## 安全与兼容

- `_external_citations` 继续校验整个上游 reference 列表的本轮授权范围、临时附件和
  联网来源；越界引用仍走 `RAGFLOW_SCOPE_VIOLATION`，不因失败状态放宽范围。
- 协议泄露、越界引用等安全原因可以独立清除正文；这不等同于按业务状态删除正文。
- 不增加公开响应字段、状态枚举、配置项、依赖或迁移；现有 `202`、失败 HTTP/SSE、
  历史消息契约保持不变。

## 验证与回滚

服务无关的事件契约测试覆盖 usage-only 结束、缺终态、取消、失败单调、节点错误恢复、
冲突和 JSON/SSE 一致性；Gateway/PG、真实脱敏 Workflow、前端集成测试必须在固定
Python 3.13 环境补跑。回滚按 F03/F04 变更登记分别撤回即可，不需要数据迁移；已保存的
失败正文不会被自动重写。
