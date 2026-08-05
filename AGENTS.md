# Agent 开发与协作规则

## 1. 总原则

本项目是 **RAGFlow 上游源码 + 企业扩展层**，不是从零开发 RAG 平台。任何 Agent 在编码前必须先判断：

1. RAGFlow 是否已有公开配置、公开 API、Connector、Provider 或 UI 能力；
2. 是否可在 `enterprise/` 或 `deploy/overlays/` 完成；
3. 是否真的必须修改上游核心。

优先级固定为：配置 > 公开 API > 外围适配器 > 独立页面 > 最小核心补丁。禁止为了“统一技术栈”替换 RAGFlow 内部数据库、文档引擎或任务执行器。

## 2. 开工前必做

每个 Agent 在修改前必须输出：

- 本任务成功标准；
- 将读取和修改的目录；
- 依赖的契约版本；
- 不会修改的共享文件；
- 验证命令；
- 主要风险。

缺少输入时，先在任务文件规定的默认假设下实现，不得自行扩大范围。

## 3. 目录所有权

| 范围 | 所有者 |
|---|---|
| 上游版本、锁文件、公共 Compose、数据库迁移、OpenAPI 基线 | Lead Agent |
| `enterprise/gateway/auth`、`enterprise/gateway/acl` | Identity/ACL Agent |
| `enterprise/gateway/sync`、对象存储适配 | File Sync Agent |
| RAGFlow ingestion 配置、解析 profile、复核服务 | Parsing Agent |
| `enterprise/gateway/query`、会话和业务查询适配 | Retrieval Agent |
| `enterprise/web` | Frontend Agent |
| `deploy/overlays`、备份、审计和安全 | Platform Agent |
| 跨模块测试、评测和验收报告 | QA Agent |

除 Lead 外，任何 Agent 不得直接修改：

- RAGFlow 官方迁移；
- 上游全局状态枚举；
- 主 OpenAPI 契约；
- 根依赖锁文件；
- 文档引擎抽象；
- 官方数据库模型。

确需修改时，提交 `CHANGE-REQUEST.md`，说明原因、替代方案、兼容风险和回滚方式，由 Lead 一次性处理。

## 4. 上游源码修改规则

每个上游修改必须：

1. 有对应 ADR；
2. 有独立测试；
3. 能以单独 commit 或 patch 表达；
4. 不与无关重构混合；
5. 记录上游文件、函数、原因和预期冲突点；
6. 在升级时可逐项重放或删除。

禁止大范围复制、改名或重排上游目录。禁止为满足本项目代码风格而格式化整仓库。

## 5. 测试要求

每个工作包至少包含：

- 单元测试；
- 与 RAGFlow API 的契约测试；
- 必要的 PostgreSQL/对象存储集成测试；
- 用户可见能力的 E2E；
- 权限相关负向用例。

测试不得使用真实客户敏感数据。禁止通过 `skip`、`xfail`、删除断言或伪造报告绕过失败。

## 6. 安全规则

- 不读取、输出或提交 `.env`、密钥、Token、Cookie 和客户数据。
- 不在日志记录完整文档正文、完整 Prompt 或原始模型响应。
- 不允许浏览器直连 MySQL、Redis、文档引擎或对象存储管理端口。
- 不允许先全库召回再删除无权限结果。
- 不允许 LLM 使用可写生产数据库账号执行自由 SQL。
- 不允许仅通过隐藏菜单实现权限。

## 7. 完成报告

每个 Agent 结束时必须报告：

- 修改文件；
- 实现行为；
- 测试命令与结果；
- 契约或配置变化；
- 尚未解决的风险；
- 是否修改上游；
- 后续集成注意事项。
