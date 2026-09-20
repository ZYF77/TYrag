# ADR-022 Workflow 通用执行诊断

- 状态：Accepted
- 日期：2026-09-20
- 上游：RAGFlow v0.26.4；补丁 RF-PATCH-013

## 问题与选择

Gateway 已请求 Agent API `return_trace`，但 Agent API 未创建普通 Chat 使用的
请求级诊断 sink。Canvas SSE 只能提供节点边界，不能关联节点内部已有的 LLM、检索
埋点。Gateway 的工具名单及输出推断不能覆盖以后新增的工具。

公开 API 没有内部执行关联信息；只修改配置或企业外围层无法补齐。因此在原 Canvas
调度、统一工具调用边界增加最小补丁，复用 ADR-017 的私有诊断通道，不引入追踪服务。

## 决策

1. Agent API 在实际消费 Canvas 的任务内建立 sink，以 `X-Request-ID` 关联 Gateway
   runId。沿用 `return_trace` 开关，没有 request ID 时不返回诊断。
2. 每次 Canvas 调度产生独立 `spanId`，携带节点 ID、名称、类型；重复执行同一节点仍有
   不同 span。ContextVar 隔离并发分支，线程池显式复制上下文。
3. 所有现有诊断事件自动继承 span，LLM 的模型、耗时、TTFT、token 用量以及检索阶段
   元数据可归属到调用节点。`parentSpanId` 表示执行嵌套关系，不等同于完整静态 DAG。
4. `LLMToolPluginCallSession` 统一记录内置工具、MCP session/binding 的调用起止、
   名称、类型、耗时、状态、参数数量、结果类型。处理抛出异常、组件吞掉的错误、MCP
   `isError`；不按工具名白名单决定是否采集。只保存异常类型，不保存异常正文。
5. partial 延迟输出保留 Canvas 的 partial 协议，真正消费时产生独立 stream span，
   归属生产节点，准备耗时不冒充模型流式总耗时。yield 时恢复消费方上下文。
6. 在节点结束、流程结束、等待输入时传回有界 checkpoint；JSON 和 SSE 均保留诊断。
   已捕获异常的响应附带私有 checkpoint，Gateway 先合并，再走原失败保存流程。
   不把累计诊断快照复制进旧的 node trace 列表。
7. Console 直接显示节点类型/用途、执行 ID、父执行、模型/工具和状态；保留展开 JSON，
   历史事件仍可读。固定错误标签替代旧 Gateway 摘要中的异常正文。

## 契约、安全与限制

- 外部 v2/OpenAPI 不变；私有 diagnostics version 1 增量增加 `workflow_node_*`、
  `workflow_node_stream_*`、`workflow_tool_*`、`workflow_error` 事件与执行关联字段。
- 沿用管理员权限、租户查询隔离、runId 匹配、有界事件数/字节数；不修改消息状态与
  citations 的关系，不从诊断推导回答业务状态。
- 不保存参数/结果正文、Prompt、知识正文、Chain-of-Thought 或凭证；不新增数据库、
  对象存储、配置项、依赖或迁移。
- 任意新增节点/工具经上述调度入口即可获得边界及已有子阶段诊断；自定义代码绕过
  公共调度执行的任意内部操作不能自动获得语义，需要调用现有安全阶段记录接口。
- 仍受 256 事件、256 KiB 上限约束；页面明确显示截断。嵌套耗时重叠，不能直接求和。
  异常进程退出/断网只能保留此前送到 Gateway 的 checkpoint，不能恢复未发送的事件。
- 不追溯补齐历史运行。本次不包含 Go runtime、第三方观测平台或外部工具服务内部链路。

## 升级与回滚

上游冲突点及函数见 RF-PATCH-013 登记；补丁可独立于企业前端变更重放。
回滚上游入口/工具/Canvas 补丁及新增模块即可退回原始节点 SSE；Gateway 关闭既有
诊断开关即可停止新数据。已保存的私有诊断无需迁移。
