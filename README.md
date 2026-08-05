# RAGFlow 企业知识库二次开发交接包

> 基线日期：2026-08-04  
> 推荐上游基线：RAGFlow `v0.26.4`，正式实施时必须再次核对官方 release、source tag、Docker image digest 与迁移脚本一致性。  
> 项目定位：在 RAGFlow 通用 RAG 能力之上建设客户专属的身份、权限、文件同步、业务数据联邦查询和业务化前端，不重新实现完整 RAG 平台。

## 1. 交接包用途

本目录应与 RAGFlow 源码一起进入新项目仓库，作为：

- 架构和范围基线；
- Agent 并行开发的任务依据；
- 接口和数据契约基线；
- 测试、评测和验收依据；
- 后续升级、审计和风险评审依据。

本交接包不包含旧 MVP 代码，也不要求迁移旧 MVP 的自研 FastAPI、PDF 解析、Qdrant、BM25、RRF、会话或前端实现。旧资料只用于提取仍然有效的业务需求。

## 2. 必须先读

1. [`docs/00-项目总纲与执行摘要.md`](docs/00-项目总纲与执行摘要.md)
2. [`docs/01-目标架构与职责边界.md`](docs/01-目标架构与职责边界.md)
3. [`docs/02-范围优先级与禁止事项.md`](docs/02-范围优先级与禁止事项.md)
4. [`docs/11-实施阶段与并行Agent方案.md`](docs/11-实施阶段与并行Agent方案.md)
5. [`AGENTS.md`](AGENTS.md)

## 3. 已锁定的默认决策

| 决策 | 默认结论 |
|---|---|
| RAGFlow 角色 | 文档解析、切片、索引、检索、引用、Chat/Session 引擎 |
| 企业业务能力 | 放在独立 `enterprise/` 集成层，不塞入 RAGFlow 核心 |
| RAGFlow 内部数据库 | 保留官方支持的数据库，不替换成业务 PostgreSQL |
| 文档引擎 | MVP 默认 Elasticsearch；Infinity 只能通过 ADR 改选 |
| Qdrant | 不纳入 MVP 基线，不改造成 RAGFlow 文档引擎 |
| 原始文件 | 上游业务系统为业务留档权威；RAGFlow 管理知识处理副本 |
| SSO | 客户入口由企业网关校验；RAGFlow 管理后台可配置 OIDC |
| ACL | 业务系统/业务 PG 为权威，检索前过滤、返回前复核 |
| 结构化业务数据 | 保留在业务 PostgreSQL，通过受控查询服务联邦查询 |
| Agent/深度研究/联网 | MVP 不开放，不作为主业务链路 |
| Text-to-SQL | MVP 不做；结构化查询使用白名单 Query Adapter |
| 前端 | 普通用户使用业务化界面，不直接暴露完整 RAGFlow 控制台 |

## 4. 推荐仓库布局

```text
repo-root/
├─ <RAGFlow upstream source>
├─ enterprise/
│  ├─ gateway/                 # 身份、ACL、文件同步、查询聚合、审计
│  ├─ web/                     # 客户业务化前端或前端覆盖层
│  ├─ adapters/                # 业务 PG、对象存储、RAGFlow API 适配器
│  └─ tests/
├─ deploy/
│  ├─ overlays/                # Compose/K8s 覆盖，不直接污染上游基线
│  ├─ env/
│  └─ scripts/
├─ patches/                    # 必须修改上游时，每项补丁独立记录
├─ docs/
│  └─ handoff/                 # 本交接包
└─ artifacts/                  # 测试、评测、审计、SBOM 输出，默认不提交
```

若 RAGFlow 源码直接位于仓库根目录，本交接包可放到 `docs/handoff/`，`enterprise/`、`deploy/overlays/` 和 `patches/` 作为新增顶级目录。

## 5. 文档索引

- `00`：总纲、目标、完成定义。
- `01`：目标架构、系统职责和数据所有权。
- `02`：P0/P1/P2 范围、明确不做和删除项。
- `03`：版本冻结、源码管理、升级和补丁策略。
- `04`：SSO、用户映射、RBAC、ACL 和权限防线。
- `05`：文件事件、同步、去重、版本、删除和状态回写。
- `06`：OCR、图片、表格、解析路由和人工复核。
- `07`：检索、问答、引用、会话和多轮上下文。
- `08`：业务 PostgreSQL 联邦查询与综合回答。
- `09`：前端业务化、角色菜单和功能隐藏。
- `10`：部署、安全、审计、备份、可观测性和升级。
- `11`：MVP/Beta/Production 阶段和并行 Agent 工作包。
- `12`：测试、离线评测、性能和验收门禁。
- `13`：风险、待决策项和 ADR 规则。
- `99`：官方事实依据、术语和来源链接。

## 6. 契约文件

- `contracts/integration-openapi.yaml`：企业集成 API 初始契约。
- `contracts/metadata-schema.json`：文档 metadata 规范。
- `contracts/error-codes.yaml`：稳定错误码。
- `contracts/status-state-machine.md`：同步、文档和问答状态机。
- `contracts/acl-policy-examples.json`：ACL 编译示例。

## 7. Agent 工作包

`tasks/` 下每个文件是一份可独立交给 Agent 的实现任务。Lead 必须先完成 WP-00，之后才允许并行启动其他工作包。
