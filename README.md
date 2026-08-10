# RAGFlow 企业知识库二次开发交接包

> 基线日期：2026-08-05
> 推荐上游基线：RAGFlow `v0.26.4`，正式实施时必须再次核对官方 release、source tag、Docker image digest 与迁移脚本一致性。  
> 官方上游 commit：`cb93883f3f8c975eecb2fed81210effeb3bdb06f`（`v0.26.4` tag）。
> 项目定位：在 RAGFlow 通用 RAG 能力之上建设客户专属的身份、权限、文件同步和业务数据联邦查询能力，不重新实现完整 RAG 平台。正式 UI/用户体验由设备管理系统负责；`enterprise/web` 仅作为 Integration Test Harness、Demo UI 和 Diagnostics UI。

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
| 正式 UI/UX | 由设备管理系统负责并通过 Enterprise Gateway 集成；普通用户不直接使用 RAGFlow 控制台 |
| `enterprise/web` | 仅 Integration Test Harness + Demo UI + Diagnostics UI，不是正式客户前端 |
| External v2 | `2.0.0` integrated candidate；Asset Registry 是 equipmentId 唯一权威，真 SSE 和 durable run 已纳入 P0 |
| Gateway 状态 | 当前候选使用 Enterprise 自有 SQLite，面向单 Gateway/多 worker；生产多副本 PostgreSQL repository 另立任务 |
| replay protection | 生产使用独立 Redis/Valkey `SET NX EX 600`；仅显式测试模式允许内存实现，Redis 不可用时 fail closed |

## 4. 推荐仓库布局

```text
repo-root/
├─ <RAGFlow upstream source>
├─ enterprise/
│  ├─ gateway/                 # 身份、ACL、文件同步、查询聚合、审计
│  ├─ web/                     # Integration Test Harness、Demo UI、Diagnostics UI
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
- `09`：设备管理系统 UI 集成要求，以及测试/演示/诊断界面边界。
- `10`：部署、安全、审计、备份、可观测性和升级。
- `11`：MVP/Beta/Production 阶段和并行 Agent 工作包。
- `12`：测试、离线评测、性能和验收门禁。
- `13`：风险、待决策项和 ADR 规则。
- `99`：官方事实依据、术语和来源链接。

## 6. 契约文件

- `contracts/integration-openapi.yaml`：企业集成 API 初始契约。
- `contracts/integration-openapi-v2.yaml`：设备管理系统使用的 v2.0.0 wire 契约；当前仓库实现为 integrated candidate，v1 保持兼容。
- `contracts/external-integration-contract-freeze-v2.md`：P0 决策、优先级与验收门禁。
- `contracts/metadata-schema.json`：文档 metadata 规范。
- `contracts/error-codes.yaml`：稳定错误码。
- `contracts/status-state-machine.md`：同步、文档和问答状态机。
- `contracts/acl-policy-examples.json`：ACL 编译示例。
- `contracts/acl-design-freeze.md`：P0 ACL 冻结规则与 fail-closed 约束。

## 7. Agent 工作包

工作包任务清单由 Lead 在版本库内维护，入口见 [`docs/11-实施阶段与并行Agent方案.md`](docs/11-实施阶段与并行Agent方案.md)。`tasks/*.md` 为本地工作草稿，默认不进入版本控制。
