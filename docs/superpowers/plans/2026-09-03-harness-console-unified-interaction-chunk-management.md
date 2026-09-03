# Harness / Console 统一交互与解析 Chunk 管理

## 状态

实现已落地到当前工作区；本地前端测试、构建和视觉检查已完成，后端数据库测试仍需可用的测试 PostgreSQL 后再执行。30 服务器发布在本地验证收口后执行。

本轮仅完成工作区前端视觉收口，未读取或修改 30 服务器 `.env`，也未执行 30 发布。

## 范围与约束

- 修改范围：`enterprise/web`、`enterprise/gateway` 的管理端适配、对应测试，以及根目录 `AGENTS.md` 的可复用交互规范。
- 保持公共 v2/v3 API、RAGFlow 核心、数据库迁移和浏览器直连边界不变。
- reasoning 只展示 Gateway/RAGFlow 已脱敏的阶段摘要；管理端消息接口不回放原始 reasoning。
- 文档与 Chunk 读取走 Gateway 隐藏 admin 路由，按租户和 admin capability 隔离，不返回凭证、路径、hash、ACL、原始错误堆栈或原始 Prompt/知识正文。

## 实现清单

- [x] 新增 `ConsoleOverlay`：居中 Dialog、锚点 Popover、左键外部点击、Escape、焦点恢复和浅遮罩。
- [x] 新增 `PaginationBar`：默认每页 20，支持 20/50/100，分页固定在面板底部。
- [x] Harness 双栏会话工作区：固定高度、独立滚动、设备创建/换绑居中弹窗、设备编号字段、复制会话 ID fallback。
- [x] Harness 输入工具栏：左侧附件和 Globe 图标，右侧中文推理档位滑块；后端继续传递原有枚举。
- [x] Harness `reasoning.delta` 增量缓冲、三角展开、思考中状态、历史 reasoning 回放；正文、替换回答和 citation 分开处理。
- [x] citation 角标单片段查看、来源按钮片段组查看、RAGFlow crop 图片展示。
- [x] Console 会话/文件元数据/会话管理/RAG 诊断/HTTP 日志的行查看、固定高度详情弹窗、独立消息/JSON 滚动、分页和操作列固定。
- [x] 高级检索和 HTTP 时间范围改为圆角弹窗；诊断 JSON 默认收起，查询显示“规范化查询（RAGFlow 实际检索文本）”。
- [x] 新增文档优先的“解析 Chunk”页，以及文件详情、解析配置投影、解析状态、Chunk 分页和 Chunk 详情。
- [x] 新增隐藏 admin 路由：文档列表、文档详情、文档 Chunk 列表；RAGFlow 不可用返回安全错误。
- [x] 根 `AGENTS.md` 写入统一弹窗、筛选、分页、表格和敏感数据展示规则。
- [x] 根据首轮视觉复核补齐 UI 收口：Harness/Console 头部同尺寸、Portal 弹窗变量与按钮可读性、详情固定头部/独立滚动、表格分页固定、账号菜单外部关闭、Globe thumb 和会话草稿清理。

## 验证记录

- `cd enterprise/web; npm run test`：通过（22 个测试文件，169 个测试）。
- `cd enterprise/web; npm run build`：通过。
- `python -m compileall -q enterprise/gateway/admin_router.py enterprise/gateway/query/v2_router.py`：通过。
- 浏览器视觉检查：本地 Harness 临时端口检查 1465×794、1672×941、1113×794 及窄屏布局；确认设备/时间范围/HTTP 详情弹窗定位、按钮颜色、Globe thumb、分页位置和窄桌面折叠。HTTP 详情恢复深色诊断卡片，并修复 1060px 弹窗被默认 960px 容器裁切的问题。
- `pytest enterprise/tests/test_system_admin.py -q`：测试夹具默认连接 `127.0.0.1:55432`，当前未监听，因 PostgreSQL 连接拒绝而未进入用例；不是代码断言失败。
- 本机 Docker 联调（`tyrag-production`）：使用 `tyrag/enterprise-web:v0.26.4-local-ui`，Gateway 以工作区 `enterprise`/`contracts` 只读挂载运行；RAGFlow 使用本机配置模板生成容器内依赖地址。`enterprise-web`、`enterprise-gateway`、`ragflow-cpu` 及 MySQL/Redis/ES/MinIO/PostgreSQL 共 8 个容器均为 healthy；`3000` 页面 200、Gateway health 200、RAGFlow ping 返回 `pong`。首次启动发现 production Compose 未挂载 RAGFlow 配置模板且未传 `API_PROXY_SCHEME`，仅通过未入库的临时 override 修正后已删除；未修改正式 Compose、数据卷或 30 环境。
- 本机页面静态检查：实际 bundle 含“思考中”和 `harness-toggle-thumb`，不含已移除的 `GATEWAY · PUBLIC API` 文案；登录页因未提交本地会话凭证保持未登录，仅做静态/健康检查。
- 本机 Console 登录来源校验：空的 `ENTERPRISE_CONSOLE_ALLOWED_ORIGINS` 会使 Web `Origin: http://127.0.0.1:3000` 返回 `403 CONSOLE_CSRF_REJECTED`；加入精确的 `127.0.0.1:3000`/`localhost:3000` 后，同一请求进入凭证校验（故意使用无效密码得到 `401 CONSOLE_AUTH_INVALID`），非信任来源仍为 403。30 环境疑似同一配置缺口，本轮未连接或修改；本机复现用 override 保存在被忽略的 `tmp/local-origin-test.yml`。
- 本机运维口令恢复：通过本机安全提示生成约定口令的 scrypt hash，写入被忽略的 `tmp/local-console-password-hash.env` 并重建 Gateway；容器内 hash 与文件一致，明文口令未写入仓库、日志或回复。Gateway/Web/RAGFlow 仍为 healthy。

## 发布门禁

完成测试数据库验证后，按 `docs/integration/update-30-server-agent.md` 只发布本次相关的 `enterprise-web` 与 `enterprise-gateway`。recreate 后从开发机验证 30 的 `0.0.0.0:3000`、`0.0.0.0:5188`、HTTP 可达性和线上 marker；不带入无关 dirty changes。
