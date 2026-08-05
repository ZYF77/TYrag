# RAGFlow 企业知识库二次开发完整交接文档

> 本文件是分拆文档的合并阅读版；Agent 实施仍以各独立文档和 `tasks/` 工作包为准。


---

<!-- Source: README.md -->

# RAGFlow 企业知识库二次开发交接包

> 基线日期：2026-08-04  
> 推荐上游基线：RAGFlow `v0.26.0`，正式实施时必须再次核对官方 release、source tag、Docker image digest 与迁移脚本一致性。  
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


---

<!-- Source: docs/00-项目总纲与执行摘要.md -->

# 00 项目总纲与执行摘要

## 1. 业务背景

客户在既有设备全生命周期/项目管理系统中维护设备、固定资产、报修、维修、保养和附件。附件以扫描版 PDF 为主，也可能包含数字 PDF、混合 PDF、表格、流程图、设备示意图和证书。用户从既有业务系统进入知识问答，回答需要同时利用：

- 设备文档中的非结构化知识；
- 业务 PostgreSQL 中的设备事实、维修和保养记录；
- 当前用户、部门、角色和设备范围权限；
- 当前会话绑定的设备、故障码或业务上下文。

## 2. 建设目标

建设一套本地或客户内网部署的企业知识库能力，使用户能够：

1. 在原业务系统上传和维护文件；
2. 文件事件可靠同步到 RAGFlow；
3. 扫描、数字和混合 PDF 被解析、切片和索引；
4. 设备号、固定资产号、故障码和自然语言均可检索；
5. 回答提供文档、版本、页码、片段、图片或表格证据；
6. 结构化事实从业务 PG 查询，不依赖向量猜测；
7. 所有召回、引用、文件和会话均执行服务端权限控制；
8. 失败可诊断、可重试、可审计；
9. RAGFlow 可持续升级，不形成难以维护的深度 Fork。

## 3. 核心建设策略

### 3.1 复用 RAGFlow

复用其原生的：

- 文档解析和 ingestion pipeline；
- OCR、PDF parser、图片和表格能力；
- Chunk、父子 Chunk、metadata；
- Dense/全文混合检索、Reranker；
- Chat、Session、Memory 和流式输出；
- 模型管理、数据源 Connector 和管理界面。

### 3.2 新增企业集成层

新增 `enterprise/`，实现：

- 客户 SSO Token 验证与用户映射；
- 业务 RBAC/ACL 策略编译；
- 上游文件事件、去重、版本和删除同步；
- RAGFlow API 适配与兼容保护；
- 业务 PostgreSQL 白名单查询；
- 文档证据与业务数据聚合；
- 业务会话映射、审计、限流和统一错误码；
- 普通用户业务化前端。

## 4. 完成定义

### 4.1 MVP 完成

- 固定 RAGFlow tag、镜像 digest 和迁移基线；
- 真实扫描、数字和混合 PDF 闭环；
- 文件事件幂等同步、版本更新和停用；
- 基于业务身份的检索前 ACL 过滤和结果复核；
- 设备台账、维修和保养记录的受控查询；
- 多轮会话、设备上下文和流式回答；
- 可打开的 PDF/图片/表格引用；
- 20-50 个真实脱敏问题的离线评测；
- 关键 E2E、越权和失败恢复测试通过。

### 4.2 Beta 完成

- 批量历史文件迁移；
- OCR/metadata 人工复核队列；
- 文档删除和版本同步收敛；
- 业务记录与文档综合回答；
- 检索评测页面、完整审计和基础运维面板；
- 备份、恢复脚本和大文件压力测试。

### 4.3 Production 完成

- 高可用和容量规划；
- 异地备份与真实恢复演练；
- 安全测试、SBOM 和许可证治理；
- 模型/索引升级、评测、切换和回滚；
- 生产性能门禁和运维 Runbook。

## 5. 重点结论

- 不把业务 PG 与 RAGFlow 内部数据库合并。
- 不以 Qdrant 替换 RAGFlow 文档引擎作为 MVP 任务。
- 不重写 PDF 解析、BM25/RRF、Chat Session 或模型管理中心。
- 不把 Agent、深度研究、联网搜索和 Text-to-SQL 放入主流程。
- 前端隐藏不是权限；权限必须由服务端执行。
- 原始业务文件与 RAGFlow 知识副本有不同职责，零拷贝不是验收目标。


---

<!-- Source: docs/01-目标架构与职责边界.md -->

# 01 目标架构与职责边界

## 1. 逻辑架构

```text
┌──────────────────────────────────────────────┐
│ 客户业务系统                                  │
│ 用户、部门、角色、设备、附件、维修、保养、入口 │
└───────────────────┬──────────────────────────┘
                    │ SSO Token / 文件事件 / 问答
                    ▼
┌──────────────────────────────────────────────┐
│ Enterprise Integration Gateway               │
│ 身份映射、ACL、同步、业务查询、会话映射、审计  │
└───────────┬───────────────────┬──────────────┘
            │                   │
            ▼                   ▼
┌──────────────────────┐  ┌──────────────────────┐
│ RAGFlow              │  │ 业务 PostgreSQL      │
│ 解析/索引/检索/问答   │  │ 设备/资产/维修/保养  │
└───────────┬──────────┘  └──────────────────────┘
            │
            ├─ RAGFlow DB
            ├─ Redis/Valkey
            ├─ Elasticsearch（默认）/Infinity
            └─ S3-compatible Object Storage
```

## 2. 数据所有权

| 数据 | 权威系统 | RAGFlow 是否保存 | 说明 |
|---|---|---:|---|
| 用户密码和主账号 | 客户身份系统 | 否 | RAGFlow 管理后台可有映射用户 |
| 用户角色/部门 | 客户业务系统 | 可缓存最小映射 | 权威规则不迁移到 RAGFlow |
| 设备和固定资产 | 业务 PG | metadata 仅存检索必要字段 | 不能把完整台账向量化替代数据库 |
| 报修/维修/保养 | 业务 PG | 可选同步非结构化摘要 | 精确记录仍由业务 PG 返回 |
| 原始业务附件 | 上游对象存储 | 可保留知识副本 | 上游负责业务留档和生命周期 |
| Chunk/索引/解析派生物 | RAGFlow | 是 | 可重建派生数据 |
| 外部 ID 映射 | 企业集成库 | 否 | 禁止直接写 RAGFlow DB 建映射 |
| 会话正文 | RAGFlow 或业务系统二选一 | 依选型 | MVP 默认 RAGFlow Session 为正文源 |
| 会话业务上下文 | 企业集成库 | 最小变量可传入 | 设备、故障码、业务范围 |
| 审计事件 | 企业集成库/审计平台 | 可保留 RAGFlow 原生日志 | 用户级业务审计必须独立可靠 |

## 3. RAGFlow 内部职责

- 管理 dataset/knowledge base、document、chunk、chat、session；
- 运行解析器和 ingestion pipeline；
- 调用 OCR/VLM、Embedding、Reranker、Answer 模型；
- 在文档引擎中执行全文、向量和 metadata 检索；
- 返回可追溯的文档证据；
- 管理自身数据库迁移和任务执行。

## 4. 企业集成服务职责

- 仅接受客户已认证 Token 或系统间凭据；
- 将业务用户映射至 RAGFlow 用户/团队/资源；
- 计算用户可访问知识库、设备、部门和密级；
- 为每次检索构造 metadata filter 或限定文档集合；
- 对引用和文件访问再次复核；
- 接收文件事件，维护外部 ID、版本、SHA256、RAGFlow ID 和状态；
- 将结构化问题路由至业务查询适配器；
- 统一对前端输出 SSE、错误码、引用模型和审计字段；
- 隔离 RAGFlow API 变动。

## 5. 对象存储边界

推荐同一 S3-compatible 集群内分离：

```text
business-archive/     # 上游业务原始文件
ragflow-managed/      # RAGFlow 知识副本和派生文件
enterprise-temp/      # 临时附件、导入中间件，设生命周期
```

不同 bucket 使用不同凭据和策略。禁止业务前端持有 RAGFlow 对象存储管理凭据。RAGFlow 是否复制原文件由其 Connector/API 行为决定；系统不得假设“直接指向路径等于零拷贝”。

## 6. 网络边界

- 对外仅暴露业务 Web/BFF；
- RAGFlow 管理 UI 仅管理员网络可达；
- MySQL、Redis、文档引擎和对象存储默认不映射局域网端口；
- 调试 overlay 只能绑定 `127.0.0.1`；
- 外部模型端点必须进入允许清单；
- 远程模型仅发送当前用户可访问且回答所需的最小证据。


---

<!-- Source: docs/02-范围优先级与禁止事项.md -->

# 02 范围优先级与禁止事项

## 1. P0：MVP 必须实现

### 平台基线
- 固定 RAGFlow `v0.26.0` 或经评审后的稳定 tag；
- source tag、镜像 digest、entrypoint、Compose 和迁移一致；
- 默认 Elasticsearch 文档引擎；
- 真实模型 Provider 配置和连接测试。

### 企业集成
- SSO Token 验证、用户映射；
- 文件 upsert、版本、停用、重试和状态查询；
- 外部文档 ID 与 RAGFlow document/dataset 映射；
- metadata Schema；
- ACL 策略编译与返回前复核；
- 业务 PG 设备、维修、保养白名单查询；
- 会话映射和设备上下文；
- 普通用户问答页面和引用抽屉；
- 基础审计、requestId 和安全错误。

### 质量
- 扫描、数字、混合 PDF；
- 流程图、图片、表格真实样本；
- 设备码/故障码检索；
- 无权限、无答案、模型失败和文件失效测试；
- 离线评测集。

## 2. P1：Beta

- 历史档案批量迁移；
- 设备档案包和目录冲突；
- OCR/metadata 人工复核；
- 解析器路由和低置信度规则；
- 业务事实与文档证据综合回答；
- 文档删除、版本和权限变化自动收敛；
- 检索测试/评测 UI；
- 完整操作审计；
- 基础 Grafana 或等价监控；
- 备份、恢复和容量告警。

## 3. P2：Production

- 多实例、高可用和故障转移；
- 异地备份和恢复演练；
- 模型/Embedding/索引代际迁移；
- 影子评测、切换和回滚；
- 安全扫描、SBOM、许可证门禁；
- 受控结构化问数扩展；
- 必要时评估视觉向量、GraphRAG、RAPTOR 或 Agent。

## 4. MVP 明确不做

- 用 PostgreSQL 替换 RAGFlow 内部 MySQL/OceanBase 路线；
- 用 Qdrant 替换 RAGFlow 文档引擎；
- 自研 PDF/OCR/表格主解析链路；
- 自研 BM25、RRF、Reranker 框架；
- 自研完整 Chat/Session/Memory；
- 自研模型管理中心；
- 开放 Browser、MCP、Sandbox、Code Executor；
- 深度研究和联网搜索；
- 任意 Text-to-SQL；
- 完整多租户运营后台；
- 视觉向量和页面多向量；
- 将所有表格转成业务 PG 表；
- 在 MVP 建设完整 Loki/Tempo/Alloy 体系作为阻断项。

## 5. 必须删除的旧设计假设

- “既然业务数据库是 PG，所以 RAGFlow 也必须改 PG”；
- “已有 MinIO 路径，所以 RAGFlow 可直接零拷贝引用任意对象”；
- “隐藏菜单即可实现权限”；
- “全部业务记录都应写入向量库”；
- “每个内部组件都要重新抽象 Provider”；
- “先替换 Qdrant，再实现业务能力”；
- “上传完成等于处理完成”；
- “LLM 可直接 JOIN 或自由查询生产业务库”。


---

<!-- Source: docs/03-版本冻结源码管理与升级.md -->

# 03 版本冻结、源码管理与升级

## 1. 基线规则

RAGFlow 迭代较快，API、任务执行、Provider、前端和迁移持续变化。开发启动时必须提交：

```text
UPSTREAM_TAG=v0.26.0
UPSTREAM_COMMIT=<full sha>
RAGFLOW_IMAGE=<registry>/ragflow@sha256:<digest>
DOC_ENGINE=elasticsearch
DB_MIGRATION_BASELINE=<migration identifier>
HANDOFF_SPEC_VERSION=1.0
```

若选择后续版本，必须创建 ADR，重新运行兼容测试，不允许只改镜像 tag。

## 2. 启动一致性检查

必须自动检查：

- Git tag 与运行时版本输出一致；
- Docker image digest 与发布清单一致；
- 本地 `entrypoint.sh` 与镜像版本匹配；
- `.env`、`service_conf.yaml.template` 和 Compose 的变量一致；
- 数据库迁移已完成；
- 文档引擎、对象存储、Redis 和数据库可用；
- API 健康检查成功后再开放前端。

## 3. 分支策略

推荐：

```text
upstream/main        # 只跟踪官方
vendor/v0.26.0       # 固定导入基线
enterprise/main      # 项目集成主线
feature/*            # 工作包分支
upgrade/vX.Y.Z       # 上游升级分支
```

也可使用单仓库，但必须保留上游 remote，并通过 merge/rebase 明确区分官方变化和项目补丁。

## 4. 核心补丁登记

`patches/manifest.yaml` 至少记录：

```yaml
- id: RF-PATCH-001
  upstream_files: []
  reason: ""
  adr: "decisions/ADR-xxx.md"
  tests: []
  introduced_in: ""
  removed_in: null
  upgrade_notes: ""
```

## 5. 升级流程

1. 阅读官方 release notes、breaking changes 和迁移文档；
2. 创建独立升级分支和环境副本；
3. 备份 RAGFlow DB、对象存储和文档引擎；
4. 升级官方源码和依赖，不先重放企业补丁；
5. 运行官方启动和迁移；
6. 运行企业 API 契约测试；
7. 逐项重放补丁并判断能否删除；
8. 运行解析、检索、ACL、会话、文件下载和 E2E；
9. 使用固定评测集比较 Recall、引用和延迟；
10. 通过后再切换，保留回滚窗口。

## 6. 禁止事项

- 直接基于 `nightly` 投产；
- 在生产数据库首次尝试迁移；
- 升级时自动删除旧 volume、旧索引或旧对象；
- 将源代码、镜像、entrypoint 和数据库版本混用；
- 将大量业务逻辑散落到上游目录且无补丁登记。


---

<!-- Source: docs/04-身份SSO-RBAC与ACL.md -->

# 04 身份、SSO、RBAC 与 ACL

## 1. 身份方案

### 客户业务入口

用户在客户系统登录，业务前端调用 Enterprise Gateway，并携带客户签发的 Token。Gateway：

1. 验证签名、issuer、audience、过期时间和租户；
2. 解析 `user_id`、部门、角色和其他稳定声明；
3. 按需查询业务权限服务；
4. 映射至 RAGFlow 资源身份；
5. 不把客户 Token 直接暴露给浏览器之外的第三方服务。

### RAGFlow 管理后台

管理员可使用 RAGFlow 原生 OAuth2/OIDC 配置登录。生产上线前必须在固定版本上验证：首次用户创建、禁用用户、邮箱变化、退出、Token 过期和回调 URL。

## 2. 用户映射表

建议企业数据库表：

```text
ext_user_map
- tenant_id
- business_user_id
- ragflow_user_id
- business_subject
- status
- created_at
- updated_at
```

不得保存客户密码。RAGFlow 用户映射失败时必须拒绝访问，不能回退为共享管理员账号。

## 3. 角色

最少角色：

- `end_user`：问答、本人会话、授权引用；
- `knowledge_maintainer`：同步状态、metadata、复核、重试；
- `system_admin`：模型、知识库、权限、部署和升级；
- `auditor`：审计元数据，正文仍需文档 ACL。

## 4. ACL 权威来源

业务 PG/权限服务负责：

- 租户；
- 部门和角色；
- 可访问设备/项目；
- 文档密级；
- allow/deny 组；
- 文档有效状态。

RAGFlow metadata 和 dataset/team 权限是执行载体，不是全部权威。

## 5. 三道防线

### 防线 A：入口权限

校验用户是否允许使用知识问答、指定设备和指定知识库。

### 防线 B：召回前过滤

将权限编译成：

- allowed dataset IDs；
- document IDs（仅数量可控时）；
- metadata filters：tenant、department、equipment、security level、status；
- 禁止先全库召回再后删。

### 防线 C：返回前复核

引用、PDF、页图、图片、表格和历史消息返回前，再用业务 PG 当前权限复核。权限变化不应等待重新 Embedding 才生效。

## 6. deny 优先规则

默认：

1. 租户不匹配直接拒绝；
2. 文档 disabled/superseded/review_required 不可检索；
3. security_level 超出用户等级拒绝；
4. `deny_group_ids` 命中优先于 allow；
5. 有 allow 规则时至少命中一项；
6. 无规则文档不得默认全员可见，除非知识库明确 public。

## 7. 必测越权路径

- 检索 API；
- Chat API；
- 文档详情；
- 原文件下载；
- Range 请求；
- 页图、缩略图和资产；
- 会话读取和删除；
- documentId、sessionId、assetId 枚举；
- 管理 API；
- 缓存命中后的权限变化。


---

<!-- Source: docs/05-文件同步版本与生命周期.md -->

# 05 文件同步、版本与生命周期

## 1. 推荐模式

上游系统仍负责用户上传和业务留档。Enterprise Gateway 通过事件同步至 RAGFlow，不能要求用户重复上传。

```text
业务上传成功
→ 写业务文件记录和 Outbox
→ 推送 document.upserted
→ Gateway 幂等消费
→ 校验对象、hash、metadata 和权限
→ 调用 RAGFlow Connector/API
→ 跟踪解析状态
→ 回写 ready/review_required/failed
```

## 2. 事件契约

必要字段：

- `event_id`：全局唯一；
- `event_type`：upsert/disable/restore/delete/reindex；
- `source_system`；
- `external_document_id`；
- `source_version_id`；
- `bucket`、`object_key` 或受控下载 URL；
- `sha256`、大小、文件名、媒体类型；
- 设备和文档 metadata；
- 权限字段；
- `occurred_at`。

## 3. 幂等

主幂等键：

```text
source_system + external_document_id + source_version_id
```

内容去重键：

```text
tenant_id + dataset_id + sha256
```

相同事件重放必须返回既有结果，不重复创建 RAGFlow 文档、解析任务和知识副本。

## 4. 外部映射表

```text
ext_document_map
- tenant_id
- source_system
- external_document_id
- source_version_id
- sha256
- ragflow_dataset_id
- ragflow_document_id
- ragflow_task_id
- lifecycle_status
- pipeline_status
- last_error_code
- last_sync_at
- source_updated_at
```

唯一约束：`tenant_id + source_system + external_document_id + source_version_id`。

## 5. 版本语义

- 新业务版本创建新知识版本；
- 新版本 ready 前，旧版本保持可检索；
- 新版本 ready 后，切换业务当前版本并停用旧版本；
- 返回前以业务当前版本复核；
- 不允许两个版本都被当作当前版本回答；
- 旧版本按保留策略保存，不能自动物理删除。

## 6. 停用、删除和恢复

### disable
立即从普通用户检索和文件访问中排除，保留数据用于审计和恢复。

### delete
MVP 仅逻辑删除。物理删除进入独立清理任务，要求保留期、引用检查和审计批准。

### restore
恢复前重新校验上游文件、权限、版本和 RAGFlow 状态；若知识副本已不存在则重新入库。

## 7. 状态回写

至少回写：

```text
received
validated
registered
parsing
ready
review_required
failed
superseded
disabled
```

“文件登记完成”不得显示为“知识处理完成”。前端必须展示当前阶段和安全错误码。

## 8. S3 Connector 与直接 API 的选择

### S3/REST Connector 适合
- 定时或批量同步；
- 上游对象可由 RAGFlow 安全读取；
- 可接受 Connector 的扫描周期和状态模型；
- 希望复用 ETag、删除同步等能力。

### Gateway 直接调用文档 API 适合
- 需要事件级即时状态；
- 需要严格外部版本映射；
- 上游对象访问方式特殊；
- 需要复杂 ACL 和业务校验。

MVP 默认 Gateway 主控，内部可复用 RAGFlow 的通用 REST Connector 或文档 API。不得依赖未公开隐藏参数作为稳定合同。


---

<!-- Source: docs/06-解析OCR图片表格与复核.md -->

# 06 解析、OCR、图片、表格与复核

## 1. 原则

RAGFlow 负责主解析链路。本项目只做：

- 解析器 profile 选择；
- 外部 OCR/VLM Provider 适配；
- 质量检测；
- 业务字段抽取；
- 人工复核和发布门禁。

不重新实现通用 `pypdfium2 + pdfplumber + OCR + chunking` 平台。

## 2. 文档类型

P0 必须验证：

- 单栏数字 PDF；
- 双栏数字 PDF；
- 纯扫描 PDF；
- 同页数字和扫描混合 PDF；
- 含表格 PDF；
- 含流程图、设备示意图和截图 PDF；
- 旋转页；
- 加密、损坏、超大和超页数 PDF。

## 3. 解析路由

建议 profile：

| 条件 | Parser 路线 |
|---|---|
| 原生文本充足、版面简单 | 原生/Naive parser |
| 扫描或 OCR 主导 | DeepDoc 或经批准的 OCR/VLM |
| 复杂版面、长 PDF | MinerU、Docling、OpenDataLoader，按固定版本评测 |
| 高价值复杂表格 | 专业表格解析 Provider 或人工复核 |
| 低置信度/结果异常 | review_required |

解析器选择必须基于真实样本评测，不以“功能更多”作为唯一依据。

## 4. 图片和流程图

要求：

- 保留文档、版本、页码和 bbox；
- 图片 OCR、标题、相邻正文和描述进入检索文本；
- 原图/裁剪图经 ACL 后才能返回或发送多模态模型；
- 流程图回答必须保留条件分支、否定条件和警告；
- 无多模态核验时，不得仅凭历史描述编造图中步骤。

## 5. 表格

MVP 保存和返回：

- 表格所在文档、页码和位置；
- 可用的 Markdown/HTML/结构化结果；
- 表格标题和上下文；
- 表格截图或原页证据。

只有明确存在统计需求的稳定表型，才进入业务 PG 专用表。不能将所有表格一律结构化入业务库。

## 6. 质量字段

企业映射库保存：

```text
parser_profile
parser_version
ocr_model_profile
parse_started_at
parse_completed_at
page_count
chunk_count
asset_count
failed_pages
quality_status
review_status
review_reason
```

## 7. 发布门禁

以下情况不得自动 ready：

- 解析任务失败；
- 关键页面为空或明显乱序；
- 强制 metadata 缺失；
- 设备关联冲突；
- 关键表格/流程图要求但未提取；
- 上游文件已停用；
- 权限字段非法；
- 解析结果数量与状态不一致。

MVP 可先文档级复核；逐页检查点和页级人工处理放入 Beta，除非真实样本证明文档级复核无法满足验收。


---

<!-- Source: docs/07-检索问答会话与引用.md -->

# 07 检索、问答、会话与引用

## 1. 查询类型

Gateway 将问题分类为：

- `document_knowledge`：文档解释、操作步骤、故障排查；
- `business_fact`：设备属性、最近维修、保养记录；
- `hybrid`：同时需要文档和业务事实；
- `unsupported_precise_query`：尚未配置的统计或自由 SQL；
- `unsafe_or_unauthorized`。

分类可由规则优先，LLM 仅作为辅助，不能绕过权限和查询白名单。

## 2. 文档检索流程

```text
原问题
→ 提取设备号/故障码/文档类型
→ 计算 ACL 和当前版本条件
→ 调用 RAGFlow retrieval/chat
→ Rerank/父子上下文由 RAGFlow 处理
→ Gateway 复核文档与引用
→ 回答模型或流式转发
```

设备码和故障码必须保留原文，同时进行规范化变体，例如 `AX-200`/`AX200`。是否修改 RAGFlow tokenizer 必须由评测证明；优先使用查询增强和 metadata 条件，不先改底层检索引擎。

## 3. 回答约束

- 只根据已授权证据和业务查询结果回答；
- 无可靠证据时明确未找到；
- 区分“文档要求”和“实际业务记录”；
- 不将模型隐藏推理返回前端；
- 故障流程保留前置条件、分支和安全警告；
- 精确日期、次数和状态以业务数据库为准；
- 回答至少包含一个有效来源，纯拒答除外。

## 4. 引用统一模型

```json
{
  "sourceType": "document|business_record",
  "sourceId": "...",
  "title": "...",
  "documentId": "...",
  "versionId": "...",
  "pageNo": 37,
  "bbox": {"x1": 0.1, "y1": 0.2, "x2": 0.8, "y2": 0.4},
  "assetId": "...",
  "excerpt": "...",
  "recordType": "maintenance",
  "recordId": "..."
}
```

客户端不得自行提交或修改 documentId、versionId、pageNo、bbox、assetId 和业务 recordId。

## 5. 会话真相源

MVP 默认：RAGFlow Session 保存消息正文；企业库保存业务映射和上下文。

```text
ext_conversation_map
- tenant_id
- business_conversation_id
- business_user_id
- ragflow_chat_id
- ragflow_session_id
- equipment_id
- fixed_asset_no
- current_fault_code
- status
- created_at
- last_message_at
```

若未来客户要求所有消息统一进入业务系统，可通过 ADR 改为业务系统真相源，但必须避免双写不一致。

## 6. 多轮上下文

每轮请求都重新验证：

- 当前用户；
- 会话归属；
- 当前设备权限；
- 设备是否已切换；
- 文档版本和状态。

设备上下文切换必须显式写入会话映射。不能因为上一轮提到设备 A，就在用户无权访问时继续召回 A。

## 7. SSE

Enterprise Gateway 可透传或转换 RAGFlow 流式输出，外部稳定事件建议：

```text
run.started
retrieval.completed
citation
answer.delta
answer.completed
run.failed
heartbeat
```

Gateway 负责：断连取消、审计、错误脱敏和事件版本。RAGFlow 内部事件变化不得直接破坏客户前端。


---

<!-- Source: docs/08-业务PostgreSQL联邦查询.md -->

# 08 业务 PostgreSQL 联邦查询

## 1. 原则

- 业务 PG 是设备事实和交易记录权威；
- RAGFlow 不直接 JOIN 业务 PG；
- LLM 不持有通用写权限或自由 SQL 权限；
- MVP 使用白名单 Query Adapter；
- 文档证据和业务记录必须分来源展示。

## 2. P0 数据域

1. 设备台账；
2. 固定资产台账；
3. 报修记录；
4. 维修记录；
5. 保养记录。

采集时序数据、复杂统计和财务信息不默认进入 P0。

## 3. Query Adapter

每个适配器定义：

- 业务用途；
- 输入 Schema；
- SQL 模板或存储过程；
- 允许字段；
- 最大时间范围；
- 最大返回行数；
- 超时；
- 数据脱敏；
- 权限条件；
- 输出 Schema。

示例：

```text
get_equipment_summary(equipment_id)
list_recent_repairs(equipment_id, limit<=20)
list_recent_maintenance(equipment_id, limit<=20)
get_fault_history(equipment_id, fault_code, from, to)
```

## 4. 综合问题流程

问题：

> AX-200 最近三次保养是什么？说明书要求多久保养一次？

执行：

1. 验证用户有权访问 AX-200；
2. `list_recent_maintenance(AX-200, 3)`；
3. RAGFlow 以 `equipment_id=AX-200` 和文档状态过滤检索保养周期；
4. 业务结果和文档证据分别结构化；
5. 回答模型明确区分“实际记录”和“手册要求”；
6. 返回业务 record citation 与 PDF citation。

## 5. 数据库安全

- 使用只读账号；
- SQL 固定参数化；
- 强制 tenant/department/equipment 权限条件；
- 设置 statement timeout；
- 限制结果行和字段；
- 禁止返回数据库内部密钥、连接信息和不必要个人信息；
- 查询审计记录 adapter、参数摘要、行数和耗时，不记录敏感正文。

## 6. Text-to-SQL 评估门槛

只有同时满足下列条件才进入 P2 评估：

- 业务方提供稳定指标定义；
- 建立只读语义层和表/字段白名单；
- 有 SQL 静态检查和成本限制；
- 有结果复核和权限注入；
- 有至少 50 个标注问题；
- 错误统计结果不会直接触发业务写操作。


---

<!-- Source: docs/09-前端业务化与功能隐藏.md -->

# 09 前端业务化与功能隐藏

## 1. 前端策略

普通用户不直接使用完整 RAGFlow 管理控制台。推荐：

- `enterprise/web` 独立业务前端；或
- 在客户现有业务系统中嵌入问答模块；
- 通过 Enterprise Gateway 调用，不直接暴露内部 API Key。

RAGFlow 控制台保留给管理员和知识维护员。

## 2. 普通用户界面

- 新会话和历史会话；
- 当前设备/固定资产上下文；
- 问题输入和流式回答；
- 文档引用和业务记录引用；
- PDF 页码定位；
- 图片、流程图和表格预览；
- 无证据、降级和权限提示；
- 会话删除。

## 3. 知识维护员界面

- 文档同步状态；
- 上游文档 ID、版本和设备关联；
- 当前解析阶段；
- 失败原因和重试；
- metadata 编辑（受控字段）；
- 人工复核；
- 停用/恢复；
- 检索测试；
- 评测问题维护。

## 4. 管理员界面

- 用户映射和角色；
- ACL 策略测试；
- RAGFlow dataset/chat 资源映射；
- 模型和 parser profile；
- 同步任务和积压；
- 备份、运行状态和升级信息；
- 审计日志。

## 5. 暂时隐藏

- Agent Canvas；
- Browser、MCP、Sandbox、Code Executor；
- Deep Research、联网搜索；
- GraphRAG、RAPTOR、PageIndex 高级选项；
- Memory 管理；
- Embedding/Reranker/Chunk 参数；
- 文档引擎和对象存储入口；
- API Key、内部 URL、对象键；
- 直接删除底层对象和索引。

隐藏仅用于减少复杂度，不能替代后端授权。

## 6. 进度语义

必须区分：

```text
上传/登记完成
等待处理
解析中
索引中
待复核
可查询
失败
已停用
```

前端不得用定时器伪造解析百分比。若 RAGFlow 只能提供阶段状态，则显示不确定进度和真实阶段，不编造数字。

## 7. 引用抽屉

默认折叠，点击后显示：

- 来源类型；
- 文档名或业务记录；
- 版本和页码；
- 命中片段；
- 图片/表格缩略图；
- “定位到原文”；
- 来源有效状态。

引用打开时必须重新鉴权，不能信任会话历史中的旧 URL。


---

<!-- Source: docs/10-部署安全审计备份与升级.md -->

# 10 部署、安全、审计、备份与升级

## 1. 部署基线

MVP 可使用 Docker Compose，但必须：

- 固定 image digest；
- 使用独立生产 `.env`/Secret；
- 仅暴露 Nginx/业务 Web/BFF 必要端口；
- MySQL、Redis、ES/Infinity 和对象存储仅内部网络；
- 设置 CPU、内存、磁盘和日志限制；
- 健康检查区分 live 和 ready；
- 先完成迁移和依赖健康再接流量。

## 2. 安全基线

- 修改所有默认密码；
- 生产使用 TLS；
- Cookie/Token 安全属性；
- 管理 UI 与普通用户入口隔离；
- 文件类型、大小、页数和压缩炸弹保护；
- 外部模型域名白名单、超时、重试和并发限制；
- Prompt/日志不包含完整敏感文档；
- 上传文件名不用于服务器路径；
- 对象存储不返回长期公开 URL；
- 禁用未使用的 Agent 工具和执行器。

## 3. 审计事件

至少记录：

- 登录/退出和身份映射；
- 文件 upsert、版本、停用、恢复和重试；
- 文档/图片/PDF 下载；
- 提问、业务查询 adapter、命中文档 ID 摘要；
- metadata 和权限变更；
- 复核决策；
- 模型、parser、知识库配置变化；
- 升级、备份和恢复演练。

审计事件只追加，业务 API 无权修改 actor、tenant、result 和 created_at。

## 4. 可观测性阶段

### MVP
- JSON 日志；
- requestId、syncEventId、documentId、sessionId；
- 基础健康检查；
- 任务积压、失败、解析和检索耗时；
- 错误码和安全脱敏。

### Beta
- Prometheus/Grafana 或现有企业监控；
- 模型超时/429/5xx；
- 文件同步延迟；
- ACL 拒绝；
- 解析、检索、首 token P95；
- 磁盘和对象存储容量。

### Production
- Trace、日志关联和告警；
- SLO 和错误预算；
- 容量预测和成本监控。

## 5. 备份范围

- RAGFlow 内部数据库；
- 文档引擎快照；
- RAGFlow 对象存储；
- Enterprise Gateway PostgreSQL；
- 配置、Secret 引用、模型 profile 和 ACL 规则；
- 审计数据；
- 固定评测集和版本清单。

仅有 Docker volume 不等于备份。备份必须离开原宿主机，并有校验清单。

## 6. 恢复演练

至少季度执行：

1. 在隔离环境恢复数据库；
2. 恢复对象和文档引擎；
3. 启动固定版本 RAGFlow；
4. 验证文档数量、解析状态、会话映射和 ACL；
5. 运行固定检索评测；
6. 输出 RTO/RPO 和失败项。

## 7. 许可证

- RAGFlow 上游为 Apache-2.0；
- 依赖、镜像、对象存储和观测组件需单独核验；
- 交付时生成第三方清单、版本、来源、许可证和修改说明；
- 不使用必须付费才能运行的功能作为基础依赖；
- 对 copyleft 组件由法务/合规确认履约方式。


---

<!-- Source: docs/11-实施阶段与并行Agent方案.md -->

# 11 实施阶段与并行 Agent 方案

## 1. 阶段 0：事实验证与基线冻结

Lead 单独完成，其他 Agent 不得并行修改共享文件。

交付：

- 固定 tag、commit、image digest；
- 本地启动和迁移成功；
- 真实 PDF 解析 POC；
- 确认文档引擎；
- RAGFlow API 能力矩阵；
- 企业目录脚手架；
- OpenAPI、metadata、错误码和状态机 v1；
- 测试环境和无敏感夹具。

退出条件：WP-00 全部通过。

## 2. 阶段 1：P0 并行实现

在 Lead 冻结契约后并行：

| 工作包 | Agent | 主要范围 |
|---|---|---|
| WP-01 | Identity/ACL | SSO、用户映射、RBAC、ACL |
| WP-02 | File Sync | 事件、幂等、版本、停用、状态回写 |
| WP-03 | Parsing | parser profile、真实样本、质量门禁 |
| WP-04 | Retrieval | Chat/Session、业务 PG、综合回答、SSE |
| WP-05 | Frontend | 普通用户、引用抽屉、维护页面 |
| WP-06 | Platform | Compose overlay、安全、审计、备份基础 |

Lead 负责跨模块模型、迁移、配置和 RAGFlow 核心补丁。

## 3. 阶段 2：集成与 Beta

- 设备档案包；
- 批量历史导入；
- 人工复核；
- 删除和版本一致性；
- 评测 UI；
- 监控、备份和大文件测试；
- 真实模型限流和降级。

## 4. 阶段 3：Production

- 高可用；
- 安全和许可证门禁；
- 灾备演练；
- 模型/索引升级；
- 性能 SLO；
- 生产运维手册。

## 5. 并行规则

- 同一共享契约只能由 Lead 修改；
- 每个 Agent 有独占目录；
- 前端基于冻结 OpenAPI 和 Mock 开发；
- 上游核心修改必须串行合并；
- QA 不接受“实现已完成”声明，必须以自动化证据判断；
- 每波结束运行全量契约和 E2E，再进入下一波。

## 6. 里程碑验收

### M0 基线
- 一条命令启动固定版本；
- 官方 UI 和 API 可用；
- 三类 PDF POC；
- 版本清单完整。

### M1 文件与权限
- 文件事件到 ready；
- 幂等和停用；
- 无权限召回和下载为 0。

### M2 问答闭环
- 文档 + 业务记录综合回答；
- Session、设备上下文、SSE 和引用；
- 真实评测集达标。

### M3 Beta
- 复核、批量、审计、备份、监控和大文件。

### M4 Production
- HA、灾备、安全、升级和 SLO。


---

<!-- Source: docs/12-测试评测与验收标准.md -->

# 12 测试、评测与验收标准

## 1. 测试层级

- 单元：ACL、幂等、状态机、查询路由、引用转换；
- 契约：RAGFlow API、业务 PG Adapter、对象存储、SSO；
- 集成：Gateway + PG + RAGFlow 测试环境；
- E2E：文件同步、问答、引用、权限、会话；
- 性能：批量同步、解析积压、检索、SSE；
- 恢复：服务重启、任务重试、备份恢复；
- 安全：IDOR、越权、Token、文件、Prompt 和日志。

## 2. 必备夹具

- 单栏数字 PDF；
- 双栏数字 PDF；
- 纯扫描 PDF；
- 混合 PDF；
- 表格 PDF；
- 流程图/设备图 PDF；
- 旋转页；
- 损坏、加密、超大、超页数 PDF；
- 两个用户、两个部门、交叉设备权限；
- 多版本同文档；
- 维修和保养结构化记录。

## 3. P0 功能门禁

### 文件
- 同一事件重复 10 次只产生一个映射和一份有效知识版本；
- 新版本 ready 前旧版本继续可用；
- disable 后在约定时间内检索和下载均不可用；
- 失败可安全重试，错误可诊断。

### 权限
- 无权限 Dense/全文/Chat 召回为 0；
- 无权限 PDF、Range、页图、资产和会话均拒绝；
- deny 优先和密级规则正确；
- 权限变化不依赖重新 Embedding。

### 解析
- 三类 PDF 到达 ready 或明确 review_required；
- 图片和表格可作为引用打开；
- 关键失败不被误报 ready；
- 上传完成和解析完成状态严格区分。

### 问答
- 每个有答案问题至少一个正确来源；
- 无证据明确拒答；
- 业务记录和文档来源分开；
- 多轮设备上下文正确切换；
- 不跨用户读取会话。

## 4. 离线评测

至少 50 题 Beta 前完成，MVP 可先 20-50 题：

- 设备码/固定资产号；
- 故障码变体；
- 操作和排查流程；
- 表格和图片；
- 跨文档；
- 无答案；
- 业务事实；
- 文档 + 业务综合；
- 权限负样本。

默认指标建议：

```text
Recall@8 >= 80%
设备码/故障码命中率 >= 95%（目标 100%）
有答案问题正确引用率 >= 90%
无权限泄漏率 = 0
无答案拒答准确率 >= 90%
引用版本正确率 = 100%
```

阈值必须由客户样本和风险确认，不能为通过验收临时降低。

## 5. 性能基线

初始目标，正式容量测试后修订：

- 排除外部回答模型，检索 P95 < 2 秒；
- SSE `run.started` < 1 秒；
- 最长 15 秒有内容或 heartbeat；
- Gateway 业务 PG 查询 P95 < 1 秒；
- 文件事件登记 < 2 秒，解析异步；
- 外部模型并发受配置限制，无无限重试。

## 6. 验收证据

输出：

```text
artifacts/test-summary.json
artifacts/acceptance-report.md
artifacts/coverage/
artifacts/e2e/
artifacts/performance/
artifacts/security/
artifacts/evaluation/
artifacts/version-manifest.json
```

报告必须列明通过、失败、跳过、耗时、环境版本和证据位置。任何安全阻断项失败时整体不得标记通过。


---

<!-- Source: docs/13-风险待决策与ADR.md -->

# 13 风险、待决策与 ADR

## 1. 最高风险

| 风险 | 影响 | 主要控制 |
|---|---|---|
| ACL 越权 | 严重数据泄漏 | 召回前过滤 + 返回前复核 + 负向测试 |
| 上游文件和索引版本不一致 | 错误回答 | 外部版本映射、旧版保留、当前版本复核 |
| OCR 质量不足 | 错误知识 | 真实样本、质量门禁、人工复核 |
| 深度 Fork | 无法升级 | 企业外围层、补丁登记、ADR |
| 文件双份生命周期失控 | 孤儿/旧数据 | 明确业务原件与知识副本职责 |
| 结构化事实向量化 | 精确结果错误 | 业务 PG Query Adapter |
| API 变动 | 集成中断 | Gateway 防腐层、契约测试、版本冻结 |
| 前端隐藏代替权限 | 可绕过 | 所有资源服务端鉴权 |
| 模型外发敏感数据 | 合规风险 | 最小证据、端点白名单、审计 |
| 默认凭据和管理端口 | 入侵风险 | Secret、内网、TLS、扫描 |

## 2. 开发前必须决策

1. 固定 RAGFlow tag/commit/image digest；
2. Elasticsearch 或 Infinity；
3. 上游文件通过 S3 Connector、REST Connector 还是直接文档 API；
4. 是否允许 RAGFlow 保存原文件知识副本；
5. 客户 Token 格式、issuer、audience；
6. 用户和租户映射规则；
7. ACL 权威表和 deny/allow 规则；
8. 第一批业务 PG 表、字段和查询 Adapter；
9. 会话正文真相源；
10. OCR/VLM、Embedding、Reranker、Answer 模型；
11. 第一批真实脱敏 PDF 和评测问题；
12. 普通用户和维护员前端嵌入方式；
13. 文档删除、保留期和审计要求；
14. 生产部署、备份和数据外发要求。

## 3. ADR 模板

```markdown
# ADR-XXX 标题

- 状态：Proposed / Accepted / Deprecated / Superseded
- 日期：YYYY-MM-DD
- 决策人：

## 背景
## 约束
## 备选方案
## 决策
## 正面影响
## 负面影响和风险
## 验证方式
## 回滚方式
## 对上游升级的影响
```

## 4. 必须创建 ADR 的事项

- 文档引擎选择或切换；
- 修改 RAGFlow 内部数据库；
- 修改文档引擎抽象；
- 引入 Qdrant；
- 变更会话真相源；
- 引入 Text-to-SQL；
- 开放 Agent/MCP/Browser/Code；
- 远程模型处理敏感正文；
- 上游核心补丁；
- 生产高可用架构。


---

<!-- Source: docs/99-官方依据与术语.md -->

# 99 官方依据与术语

## 1. 核验结论（截至 2026-08-04）

- RAGFlow 官方仓库页面标示最新稳定 release 为 `v0.26.0`，发布于 2026-06-11。
- 官方 README 要求源代码 tag 与 Docker image 版本匹配，示例使用 `v0.26.0`。
- 官方主部署依赖包括 MySQL、Redis、MinIO/S3-compatible 对象存储，以及 Elasticsearch 或 Infinity 文档引擎。
- 官方 README 明确默认使用 Elasticsearch 保存全文和向量，并可切换 Infinity。
- `service_conf.yaml` 支持 OAuth2、OIDC 和 GitHub 登录配置。
- 2025-2026 release notes 已加入：可编排 ingestion pipeline、父子 Chunk、metadata 自动生成和过滤、Memory/Session、S3 数据源增量和删除同步、通用 REST Connector、MinerU/Docling/OpenDataLoader 等。
- RAGFlow API 在持续 RESTful 重构，因此企业项目应通过 Gateway 隔离版本变化。

## 2. 官方来源

1. RAGFlow GitHub：  
   https://github.com/infiniflow/ragflow
2. Releases：  
   https://github.com/infiniflow/ragflow/releases
3. v0.26.0：  
   https://github.com/infiniflow/ragflow/releases/tag/v0.26.0
4. 中文 README：  
   https://github.com/infiniflow/ragflow/blob/main/README_zh.md
5. Docker 配置说明：  
   https://github.com/infiniflow/ragflow/blob/main/docker/README.md
6. Docker Compose：  
   https://github.com/infiniflow/ragflow/blob/main/docker/docker-compose.yml
7. Release notes：  
   https://github.com/infiniflow/ragflow/blob/main/docs/release_notes.md
8. PDF parser 选择：  
   https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/select_pdf_parser.md
9. 官方文档站：  
   https://ragflow.io/docs

## 3. 术语

- **Dataset/Knowledge Base**：RAGFlow 中的知识库资源。
- **Document Engine**：保存全文、向量和可过滤字段的检索后端。
- **Ingestion Pipeline**：文档解析、清洗、转换和切片流程。
- **Enterprise Gateway/BFF**：客户系统与 RAGFlow 之间的企业集成、防腐和安全层。
- **业务原件**：上游系统用于业务留档的权威文件。
- **知识副本**：为解析、索引和引用由 RAGFlow 管理的文件副本。
- **ACL 编译**：把业务权限转换为 dataset、document 或 metadata 检索条件。
- **返回前复核**：在引用、文件或记录返回用户前，再读取当前业务权限和版本。
- **Query Adapter**：预定义、参数化、只读的业务数据库查询能力。
- **ADR**：Architecture Decision Record，记录重要架构决策和取舍。

## 4. 事实使用注意

官方能力变化较快。任何 Agent 在实现具体 API 前必须以项目固定 tag 的源码和该 tag 文档为准，不能只按 main 分支、历史博客或未公开参数实现。GitHub issue 和第三方文章只能作为风险线索，不能替代公开 API 合同。
