# F09：安全过程展示

状态：本地实现；固定 Python 3.13 / RAGFlow 实际调用链验收未完成；未部署。

Gateway 的 Chat、旧 formal 入口只把观察到的处理阶段映射为固定“正在处理请求。”，不摘录模型推理。Workflow 继续不输出原始推理。前端只展示已认可的提示，名称改为“处理过程”。JSON/流式标签支持大小写、拆分、未闭合，并保护代码中的同名标签。未标记的旧历史 reasoning 隐藏，不清理数据库旧内容。

企业 schema 9→10 增加 `ext_v2_message.reasoning_format`，新增消息仅保存 `safe_execution_v1` 的白名单内容；run 结果不保存原始流式事件。旧正文和消息状态不改。`reasoning` / `reasoning.delta` 字段保留，语义变为安全过程提示。

最小上游补丁：
- `rag/safe_logging.py`：请求 ContextVar，在 handler 前移除日志正文、参数和异常堆栈；保留 logger、级别、时间及位置用于定位。不通过原始日志摘要生成公开内容。
- `api/db/services/dialog_service.py`：restrict Chat/Agentic 调用启用上述上下文，关闭 Langfuse；`agent/canvas.py::Canvas.run` 对携带 authorized_doc_ids 的企业 Workflow 启用。
- `api/db/services/llm_service.py::LLMBundle.__init__`：上述上下文中的模型实例禁止敏感 tracing，clone 保持该设置。
- `think_log.py` / `think_timeline.py`：未知阶段丢弃；字符串元数据必须匹配有限枚举。指标和计数不从模型正文提取。

代价：上述企业调用期间日志细节减少；异常靠既有错误码、位置和脱敏诊断排查。第三方直接 print、进程外 Provider 日志不属于 Python logging 拦截能力，真实环境须验证日志出口。不能把合成测试视为已检查线上日志。

验证：`python3 -m unittest discover -s docs/reviews/tests -p test_f09_controls.py -v`；`test_answer_split.py`；审查探针；HarnessChat 与 TypeScript。固定依赖服务集成待办。

升级重放：检查 async generator 上下文传播、模型 clone 的 disable_langfuse、Canvas inputs 形状、logging factory 与现有监控组合、流式标签协议。不能只改前端而保留后端原始流。回滚也须保持原始推理隐藏；部署前停旧 worker，迁移后配套更新 Gateway/前端。
