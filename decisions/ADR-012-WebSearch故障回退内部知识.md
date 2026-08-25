# ADR-012：WebSearch 故障回退内部知识

## 状态

已采纳，对应 RF-PATCH-005。

## 背景

EAM Query v2.9.0 只有在消息显式设置 `internetEnabled=true` 时才尝试 RAGFlow 已配置的 WebSearch provider。RAGFlow v0.26.4 的 simple chat 路径在 provider 构造或检索异常时会让整轮失败，而 agentic 路径已经能以空 Web 结果继续内部知识。

## 决策

- provider 已配置且显式启用时才尝试 WebSearch；不自动切换 provider。
- 只捕获 provider 构造、Web 检索和 Web 结果合并异常；内部知识检索异常仍按原路径失败。
- WebSearch 失败后保留已取得的内部 chunks，继续本轮生成。
- 降级日志使用固定文本；advanced RAG 检索日志只记录字符数/文档数，不记录 question、keywords、URL、key 或异常正文。
- 不改变 Query v2.9.0 字段、状态或 SSE event。

## 回滚

撤销 RF-PATCH-005 会恢复 provider 故障导致整轮失败的旧行为；无数据迁移或清理。
