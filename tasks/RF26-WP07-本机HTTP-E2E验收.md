# RF26-WP07 本机 HTTP E2E 验收

> 当前状态：`BLOCKED`。本机服务已加载并实跑；主链已到达官方 upload/chunks、质量门、JSON/SSE/history/citation 与 callback，官方 chat/session 定点检查也通过，但后续全量复跑时 RAGFlow 容器无法解析默认及已配置 embedding 域名，尚无单次全绿 artifact。复跑边界见 `docs/15-RF26-实施与本机验收报告.md`。

## 目标

在本机启动的 Gateway 与 RAGFlow v0.26.4 上，以真实 HTTP 验证收敛后的 FILE_SHARE→解析/质量→ACL retrieval→会话 JSON/SSE→citation/callback 主链；将离线/单测与本机 E2E 分开报告，不连接 30 服务器。

## 范围与非范围

范围：非敏感 fixture、服务 preflight、HTTP runner、最小安全负例、可审计 artifact、版本/端口检查。非范围：30 服务器、生产验收、客户数据、读取/回显无关 `.env` secrets、修改业务协议、为使测试变绿而 skip/xfail/删除断言。

## 真实调用链

测试以实际网络路径覆盖：`FILE_SHARE v3.1.0 HMAC register → Gateway official multipart → RAGFlow documents/chunks → Gateway quality status/callback → JWT v2 conversation/session → ACL-filtered retrieval/completion → JSON 与 SSE → Citation Snapshot/source authorization`；附件另走 `v2 multipart → documents/upload → same-run completion`。会话验收通过 RAGFlow 官方 chat/session API 完成，不在 Gateway 运行时从宿主机直读其 SQLite。所有测试请求以现有外部路径，不直连 Redis、文档引擎或对象存储管理端口。

## 接口与责任归属

QA Agent：fixture、runner、报告、断言真实性。Platform Agent：本机 compose/health/端口。各功能 Agent：修复自己目录内失败。Lead：版本/上游 patch/公共 contract 收口。EAM contracts 保持 Query v2.9.0、FILE_SHARE v3.1.0、Callback v1.0.0。

## 精确实施任务

1. preflight 证明 RAGFlow 版本为 v0.26.4、Gateway/RAGFlow health 可达，并记录非敏感地址/版本；不把 `health=healthy` 单独当业务通过。
2. 准备脱敏 PDF 和只读 FILE_SHARE fixture，执行一次 v3 register，断言唯一的 RAGFlow request 是官方 multipart，而非 external endpoint。
3. 轮询 document/chunk 与 Gateway quality，断言合格才 retrievable；记录 callback terminal payload/签名验证结果与至少一次失败重试模拟。
4. 用两个用户/组做 v2 JSON 与 SSE：授权用户命中，未授权用户既无答案证据也无 citation；验证 `clientMessageId` 重放不重复执行。
5. 验证 history/state 原样回放、`completed|no_reliable_evidence|failed` 显式一致，citations 为空不单独决定状态。
6. 验证单会话附件 upload/cleanup/隔离和 citation 再授权；失败路径不回显文件/secret。
7. 最小安全负例只保留路径逃逸、SHA-256 不匹配和 ACL/citation source 越权；业务幂等在正链重放中验证。HTTP 200 JSON 业务错误必须断言为失败。
8. 输出结果矩阵：unit/contract、本机 E2E、未执行生产/30 验收分别列出；保存不含 secrets、完整文档或完整 Prompt 的 artifact。

## 依赖、验收与回滚

依赖：WP01–WP06 与本机可运行服务。验收：上述正链、业务幂等和三个最小安全负例通过，`git diff --check` 通过，报告明确本机 E2E 不等于生产/30 接收。若任何中段失败，报告直接问题、证据、影响与最小恢复路径，不能降级为离线通过。回滚：停止本机测试服务或恢复各工作包的独立应用改动；不删除历史表、对象、索引或 artifact。

## Agent 目录所有权

QA Agent：跨模块 tests、runner、验收报告。Platform Agent：`deploy/overlays` 与本机运行。其他 Agent 不修改 QA 基线来掩盖失败；Lead 处理公共 compose、上游/契约变更。
