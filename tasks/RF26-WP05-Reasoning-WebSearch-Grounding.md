# RF26-WP05 Reasoning、WebSearch、Grounding

## 目标

收敛既有 Reasoning、WebSearch 与 Grounding 行为到 EAM Query v2.9.0 的现有 SSE/状态契约：WebSearch 默认关闭，只在既有 `internetEnabled=true` 且 provider 已配置时启用；保留 RF-PATCH-004，且不增加 Sequential-Thinking 依赖。

## 范围与非范围

范围：`reasoningMode` 的既有 `simple|low|medium|high|ultra` 映射、既有 `internetEnabled` 显式开关、已配置 provider、Web citation 区分、Grounding Guard/提示词可见性、no-reliable-evidence 事实口径、日志脱敏与回归。非范围：新增 WebSearch provider/字段、默认联网、Sequential-Thinking 主链、改 EAM URL/事件、删除 RF-PATCH-004、以 citation 是否为空改业务状态。

## 真实调用链

`EAM v2 message(reasoningMode,internetEnabled)` → Gateway 严格 DTO/ACL/quality scope → `internetEnabled=false` 只走内部知识；显式为 true 时先确认 chat 已配置 provider，再把既有参数传给 `POST /api/v1/chat/completions` → RAGFlow 现有 Reasoning/WebSearch 与 RF-PATCH-004 grounding/prompt-fit 分支 → Gateway 把内部文档和 `sourceType=web` 引用分开映射，并依据显式完成、拒答或失败事实形成业务状态。provider 不可用时回退内部知识，不暗接其他服务。

## 接口与责任归属

Gateway：EAM 参数验证、显式联网开关、企业状态、内部/外部 citation 区分、SSE 映射、审计脱敏。RAGFlow：已配置 provider 下的既有 reasoning/web-search/grounding 执行、prompt fit、模型输出。Grounding Guard 是提高事实可靠性的运行策略，不改变 ACL 的硬前置地位；`reference.chunks` 存在也不是 prompt 可见性的证明。

## 精确实施任务

1. 固定并测试每个既有 reasoningMode 的默认/允许值；未知值仍按既有 422，不添加新 mode。
2. 复核 RF-PATCH-004 的受登记文件、配置和单元测试，保证本包不删除/重排其补丁，也不恢复已明确禁止的总开关行为。
3. 将 Grounding 拒答、无检索证据和运行失败分别映射到已有业务状态，且 citations 单独存储。
4. 确认 reasoning delta 不记录完整 question、knowledge 或原始模型内容；审计只保留所需最小摘要。
5. 保持 WebSearch 默认关闭；仅接受既有 `internetEnabled=true`，仅使用 chat 已配置 provider，外部结果映射为 `sourceType=web`，不得伪装成内部文档或绕过 ACL。
6. provider 未配置时 Gateway 以 `internet=false` 走内部知识；provider 运行时失败由 `RF-PATCH-005` 保留既有内部 chunks，不改线其他 provider。advanced RAG 日志只记字符数/数量。测试 reasoningMode、no evidence、Grounding marker/prompt-fit、显式联网、默认不联网、web citation、回退与日志脱敏。

## 依赖、验收与回滚

依赖：WP04 的统一 run/SSE 状态机。验收：本机 E2E 覆盖至少一个 reasoning 模式、一个无可靠证据案例、默认无联网和显式联网/回退；没有 Sequential-Thinking 调用；RF-PATCH-004 仍独立回归通过。回滚：仅回退本包 Gateway 映射/配置到此前已测值；不删除 RF-PATCH-004，也不改变历史消息状态。

## Agent 目录所有权

Retrieval Agent：Gateway query/prompt adapter。Lead：`ragflow/**` RF-PATCH-004 与 patch 登记。Platform Agent：非敏感配置装配。禁止修改官方模型/迁移或新增外部联网依赖。
