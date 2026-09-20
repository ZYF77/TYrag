# RF-PATCH-013：Workflow 通用执行诊断

Lead 处理；ADR：[ADR-022](../decisions/ADR-022-Workflow通用执行诊断.md)。

## 必要性及替代方案

既有 Agent API/Canvas SSE 不提供节点内部执行关联；外围按工具名字枚举无法覆盖新工具。
复用 RAGFlow 已有请求级 sink，选择调度边界埋点。未引入新依赖或修改引擎/模型/迁移。

## 上游补丁清单与预期冲突点

| 文件 | 函数/位置 | 原因及升级检查 |
|---|---|---|
| `rag/diagnostics.py` | `rag_diagnostics_span`, `record_rag_diagnostics` | ContextVar 父子执行；检查 sink 协议和现有 emitters |
| `rag/workflow_diagnostics.py` | 新模块三个公共 helper | 请求生命周期、节点调度、partial 流式消费；检查 Canvas partial 协议 |
| `agent/canvas.py` | `_run_impl._run_batch._invoke_one` | 覆盖任意节点，保留信号量、线程池与上下文传播 |
| `agent/tools/base.py` | `LLMToolPluginCallSession.tool_call_async` | 覆盖工具/MCP 调度与错误状态；删除该入口的参数/结果日志 |
| `api/apps/restful_apis/agent_api.py` | `_run_workflow_session`, `_iter_session_completion_events`, `agent_chat_completion` | 新会话/继续会话、JSON/SSE、失败 envelope 保留 checkpoint；避免在旧 trace 列表复制 checkpoint |
| `api/db/services/canvas_service.py` | `completion` | 继续会话使用相同请求级 sink |

## 兼容性与回滚

仅已有 `return_trace` 且具有 `X-Request-ID` 的请求获得私有诊断；未开启时 sink 为 Noop。
保留现有 Canvas partial 判定；延迟输出包装可同时消费同步/异步生成器。
不修改消息业务状态规则。失败响应显式返回错误码并携带私有诊断，Gateway 不向公共响应暴露。

按表删除调用及 helper 即可回滚；企业 Console 能读取旧事件。源文件挂载环境重启后端，
企业前端需重新构建并更新其静态资源服务。本次没有执行容器重启或服务器部署。

## 独立验证

`ragflow/test/unit_test/rag/test_workflow_diagnostics.py` 覆盖未知节点、并发、循环、同步线程、
延迟流、开关、失败和取消；`test_workflow_diagnostics_contract.py` 编译执行原 API/工具
函数并提供合成依赖，覆盖 API JSON/SSE、继续会话和内置/MCP 工具。
这些隔离测试不替代真实模型、数据库和浏览器 E2E。
