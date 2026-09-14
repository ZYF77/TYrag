# ADR-020：EAM 首事件诊断与 Agentic 正文流式

## 状态

已接受，2026-09-11。

## 背景

Gateway 旧诊断从 run trace 创建后才开始计时，无法解释请求解析、会话锁、范围计算和 run 预留造成的首事件等待。RAGFlow 的 Agentic `rag` 工具又在 graph 完成后才把正文交给外层流，导致用户在内部已有正文时仍然等待整段结果；graph 异常还可能被转换成普通英文答案。

## 决策

1. 沿用现有 ASGI 审计中间件记录请求接收、响应头和首个非空响应体的服务端时刻，并把请求开始点传给现有 run 诊断。新增阶段只记录耗时、状态和计数，不记录 prompt、知识正文、工具参数或工具结果。
2. 保留现有 SSE 事件和字段。Console/EAM 以 `run.started` 作为处理中信号，分别展示思考和正文；`answer.replaced` 继续整体替换已经输出的正文。
3. 只在 Agentic 流式请求的 `RAGTools` 实例上启用正文回调。graph 产生的可见正文片段进入现有事件队列，工具返回值保持为空，避免外层模型再次转述同一正文；最终引用修复和授权检查仍在原路径执行。
4. Agentic graph 和终端 `rag` 工具错误穿透通用文本回退，交给现有失败状态处理；客户端取消时取消 graph 任务并清理回调。普通模型、普通工具调用和豆包 PLHD 兼容逻辑不启用该开关。
5. 不改变 `tool_choice=auto`、研究轮数、并发预算、Gateway 授权 G、metadata restrict、Evidence Contract 或供应商能力注册。

## 后果

诊断可以区分 Gateway 准备等待、run.started、首个思考、首段正文、生成阶段和总耗时；运输层时刻仍表示服务端发送时间。Agentic 正文可在 graph 完成前到达，部分输出后失败会进入失败收尾而不会落成成功答案。JSON 和历史回放继续使用完整终态；未接 SSE 的 EAM 客户端不获得增量显示。

真实 EAM 链路仍需按事件顺序验证，Gateway 合同测试需要可用的本地 PostgreSQL/Docker 环境。