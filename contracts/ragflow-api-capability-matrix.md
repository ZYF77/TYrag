# RAGFlow API 能力矩阵 (v1)

基线: RAGFlow v0.26.4 | 文档引擎: elasticsearch | 日期: 2026-08-05

## 说明

本文档盘点 RAGFlow 上游所有 API 模块，标注每个模块的企业集成状态。状态定义:

- **已封装**: enterprise/gateway 中已有适配
- **契约已定义**: contracts/integration-openapi.yaml 中已声明企业集成端点
- **需封装**: 企业场景需要但尚未适配
- **P1/P2**: 后续阶段再做
- **内部/不暴露**: 不直接对企业用户开放

## API 能力矩阵

| 模块 | 路由前缀 | 用途 | 企业状态 | 说明 |
|---|---|---|---|---|
| user_api | `/api/v1/user` | 用户注册/登录/信息 | 需封装 (WP-01) | SSO 接入后替换注册流程；LDAP/OIDC 身份映射 |
| tenant_api | `/api/v1/tenant` | 租户/团队管理 | 需封装 (WP-01) | 企业租户与业务组织映射 |
| dataset_api | `/api/v1/dataset` | 知识库 CRUD | 需封装 (WP-02/04) | 业务文档集映射；ACL 过滤 |
| document_api | `/api/v1/document` | 文档上传/解析/状态 | upsert/status/disable/restore/delete/sync-status 已实现 | integration-openapi.yaml：文档同步与生命周期端点已实现 |
| chunk_api | `/api/v1/chunk` | 切片管理 | 内部/不暴露 | 由 RAGFlow 自动管理 |
| file_api | `/api/v1/file` | 文件上传/管理 | 需封装 (WP-02) | 与对象存储桥接 |
| chat_api | `/api/v1/chat` | 对话/补全 | planned | conversations + messages:stream 仅契约，未实现 |
| search_api | `/api/v1/search` | 检索 | 需封装 (WP-04) | 带 ACL 过滤的检索 |
| agent_api | `/api/v1/agent` | Agent 画布 | P2 | MVP 不开放 |
| bot_api | `/api/v1/bot` | 对话机器人 | 需封装 (WP-04) | 嵌入式问答组件 |
| chat_channel_api | `/api/v1/channel` | 渠道配置 | P2 | |
| mcp_api | `/api/v1/mcp` | MCP 工具服务 | P2 | 后续开放 |
| memory_api | `/api/v1/memory` | Agent 记忆 | P2 | |
| task_api | `/api/v1/task` | 异步任务状态 | 需封装 | 文档处理进度查询 |
| system_api | `/api/v1/system` | 系统状态/配置 | 已封装 | ping, version, health check |
| stats_api | `/api/v1/stats` | 使用统计 | P2 | |
| connector_api | `/api/v1/connector` | 数据源连接器 | P2 | |
| models_api | `/api/v1/models` | 模型管理 | 内部/不暴露 | Admin 配置 |
| provider_api | `/api/v1/provider` | LLM Provider 管理 | 内部/不暴露 | Admin 配置 |
| file2document_api | `/api/v1/file2document` | 文件转文档 | 需封装 (WP-03) | 解析路由与复核 |
| file_commit_api | `/api/v1/file_commit` | 文件版本提交 | 需封装 (WP-02) | 与业务版本同步 |
| plugin_api | `/api/v1/plugin` | 插件管理 | P2 | |
| openai_api | `/api/v1/openai` | OpenAI 兼容 API | P1 | 兼容现有调用方 |
| dify_retrieval_api | `/api/v1/retrieval` | Dify 兼容检索 | P2 | |
| compilation_template_api | `/api/v1/template` | 编译模板 | P2 | |
| compilation_template_group_api | `/api/v1/template_group` | 模板组 | P2 | |
| langfuse_api | `/api/v1/langfuse` | Langfuse 集成 | P2 | |
| aimlapi_api | - | AIML API 集成 | P2 | |

## 企业集成 API 对照

以下契约端点对应 RAGFlow 上游能力:

| 企业端点 (contracts/integration-openapi.yaml) | 上游能力 | 封装模块 |
|---|---|---|
| POST /documents (upsert) | dataset_api + document_api | enterprise/gateway/sync/ |
| POST /documents/{id}/disable | document_api (status) | enterprise/gateway/sync/ |
| GET /documents/{id}/status | task_api + document_api | enterprise/gateway/sync/ |
| POST /conversations | chat_api (session) | planned（未实现） |
| POST /conversations/{id}/messages:stream | chat_api (completion) | planned（未实现） |
| GET /citations/{id} | chunk_api + reference | planned（未实现） |
| GET /documents/sync-status | task_api + document_api | enterprise/gateway/sync/ |

## 不在 MVP 范围的能力

- Agent 画布/工作流编排
- 深度研究/联网搜索
- Text-to-SQL
- MCP 服务暴露
- 公开发布的嵌入式 Bot
- Sandbox 代码执行

## 与旧 MVP 的差异

| 能力 | 旧 MVP (自研) | RAGFlow v0.26.4 | 迁移策略 |
|---|---|---|---|
| PDF 解析 | 自研解析器 | deepdoc (ONNX) | 由 RAGFlow 承担 |
| 文档引擎 | Qdrant + BM25 + RRF | Elasticsearch | 不再维护旧方案 |
| 检索 | 自研 rerank + RRF | RAGFlow retrieval + chunk | 通过 search_api 适配 |
| 会话 | 自研 FastAPI | RAGFlow chat_api | 通过 chat_api 适配 |
| SSO | 无 | OIDC (上游) + 企业网关 | WP-01 实现 |
| ACL | 无 | 企业网关前置过滤 | WP-01 实现 |
