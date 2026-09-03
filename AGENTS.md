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

- 用户明确确认属于本地测试/开发环境时，Agent 可以读取工作区 `.env` 中完成启动、调试和 E2E 所必需的配置；只读取当前任务需要的变量，禁止整份转储环境或无关 secret。
- 即使在测试环境，也不得在回答、终端输出、日志、异常、artifact、Postman 导出、源码或 Git 中回显、复制或持久化 password、secret、API key、Token、Cookie 等敏感值；不得用包含 secret 的命令行参数传值。
- 未经用户明确确认的环境、生产环境和客户环境仍禁止 Agent 读取 `.env` 或 secret。客户数据不得读取、输出或提交；测试必须使用非敏感 fixture。
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

## 8. 开发环境

本项目使用自定义 Docker 镜像 	yrag/ragflow:v0.26.4（基于上游 v0.26.4 源码构建），Python 源码通过 volume 挂载到容器，修改后无需重建镜像。

### 启动环境
`ash
cd ragflow/docker
docker compose --profile cpu up -d
`

### 前端改动生效
前端 web/dist 已挂载，重新构建前端后执行：
`ash
docker restart docker-ragflow-cpu-1
`

### 后端 Python 改动生效
修改 agflow/ 下任意 Python 源码后执行：
`ash
docker restart docker-ragflow-cpu-1
`

### 重建镜像（新增依赖或修改 Dockerfile 时）
`ash
docker build -t tyrag/ragflow:v0.26.4 -f ragflow/Dockerfile ragflow/
docker compose -f ragflow/docker/docker-compose.yml up -d --no-deps ragflow-cpu
`

### 访问地址
- 前端：http://localhost:8080
- API：http://localhost:9380

## 9. 消息状态与证据解耦

- `completed` / `no_reliable_evidence` / `failed` 是消息的业务状态，由运行结果显式决定；
- `citations` 只是证据数据，与业务状态相互独立；
- 禁止用 `citations` 是否为空推导消息状态，也禁止用消息状态推导 `citations`；
- 历史回放和前端展示必须原样保留已持久化的业务状态，不得按 citation 改判或抹平状态。

## 10. 更新部署到 30 联调机

用户要求把已测通改动更新到 30 服务器（`192.168.30.30`）时，必须先读并执行：

`docs/integration/update-30-server-agent.md`

硬门禁：recreate 后必须从**开发机**验证局域网端口仍为 `0.0.0.0`。只在 30 本机 `curl 127.0.0.1` 或 health=healthy **不算成功**。仓库 compose 默认 bind 是 `127.0.0.1`，用 `production.env.example` recreate 会再次把 8080/5188/3000 打成仅本机可访问。

## 11. 前端统一交互与数据表格规范

- Console 与 Harness 的弹窗、筛选和分页优先复用 `enterprise/web/src/components/console/ConsoleOverlay.tsx`、`ConsoleTableControls.tsx`；不为单页重复实现遮罩、焦点或分页逻辑。
- 小内容使用锚点 Popover，大内容使用居中 Dialog；遮罩保持浅色，不压黑整页。鼠标左键点弹窗外或按 Escape 关闭，关闭后焦点回到触发按钮；弹窗内部内容独立滚动。
- 表格默认每页 20 行，可选 50/100；分页栏固定在表格面板底部，表格主体独立滚动。操作列固定在最右侧；有查看能力的行支持整行点击、Enter/Space 键盘查看。
- 日期筛选使用原生 `datetime-local` 控件；按钮、筛选栏、分页栏和 Dialog 保持圆角、键盘焦点、窄屏、减少动效/透明度和高对比度支持。
- 继续遵守安全规则：诊断和详情只展示脱敏投影，不在浏览器或日志回显凭证、原始 Prompt、知识正文、模型 Chain-of-Thought、对象路径或哈希。
