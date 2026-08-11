# WP-04 Phase 3 Contract Freeze：业务数据接入与 RAG / SQL 融合

| 项 | 值 |
|---|---|
| 契约 ID | wp04.phase3.contract/v1 |
| 契约状态 | **FROZEN（接口与责任边界）** |
| 实施准入 | **BLOCKED（见第 12 节）** |
| 冻结日期 | 2026-08-09 |
| 企业基线 | 8e877950636cecd8e4150344400d494389fecde7（WP-04 Phase 2 PASS） |
| RAGFlow 基线 | v0.26.4；上游源码 commit cb93883f3f8c975eecb2fed81210effeb3bdb06f |
| 复用契约 | integration-openapi 1.0.0；metadata / error / status / ACL / capability v1 |
| 范围 | 仅 WP-04 Phase 3；不包含 WP-05、Phase 4 或大规模实现 |

本文中的“必须”“禁止”是验收约束。“建议”不构成冻结接口。接口冻结不表示实施已具备全部客户输入，也不表示 RAGFlow PostgreSQL Data Source 已通过企业 ACL 验收。

核心决定：

1. PostgreSQL 中的实时、精确、可聚合业务事实继续以业务库为权威源，只能通过 Enterprise Gateway 的白名单只读适配器查询。
2. 适合检索和复用的知识正文优先复用 RAGFlow 原生 PostgreSQL Data Source 完成 Document、Chunk、Embedding 和索引，不自建第二套 embedding pipeline。
3. 同一维修行可以同时产生“业务事实视图”和“知识投影视图”，但二者必须有不同的契约、证据和更新语义。
4. RAGFlow 原生 PostgreSQL metadata 当前不能作为权威 ACL 同步链路；权限必须由企业权限事实解析并在检索或 SQL 执行前收窄。
5. 当前阶段不修改 RAGFlow 上游源码。若外围适配与公开 API 无法解除硬阻塞，必须先提交 CHANGE-REQUEST 和 ADR。

## 1. Phase 3 数据边界

### 1.1 两条数据通道

| 通道 | 权威源 | 接入路径 | 允许用途 | 禁止用途 |
|---|---|---|---|---|
| 知识通道 | PostgreSQL 的知识投影视图、现有文件源 | 固定只读视图 / SELECT → RAGFlow PostgreSQL Data Source → Dataset → Document → Chunk / Embedding / ES → Gateway RAG 检索 | 说明书、SOP、可复用的故障说明、原因经验、处理方法 | 作为设备当前状态、次数、日期或业务记录真相；直接依赖原生 metadata 做 ACL |
| 事实通道 | 客户业务 PostgreSQL | Gateway → BusinessFactAdapter → 固定命名操作 → 参数化只读 SQL / 固定视图 | 精确设备信息、当前状态、维修/保养记录、时间、次数和受控统计 | Text-to-SQL、任意表查询、浏览器直连、LLM 自由 SQL、先全量读取再做权限过滤 |

### 1.2 权威性规则

- 设备当前状态、维修时间、维修次数、实际维修动作、实际保养记录等精确事实，以业务 PostgreSQL 查询结果为准。
- 说明书、SOP、经过治理的维修经验文本，以对应知识文档快照为准。
- “事实通道”的文本字段即使也被投影到 RAG，其原始记录值仍以 SQL 结果为准。
- Hybrid 回答必须分别保留 SQL 事实证据和 RAG 引用，禁止把两类证据合并成无法追溯的一条 citation。
- 消息业务状态与 evidence / citations 数量保持解耦，继续遵守 contracts/status-state-machine.md。

### 1.3 同一记录的双投影

维修记录中的故障描述、原因分析、处理方案按以下规则拆分：

- 原始记录值：属于业务事实，按 record ID 通过 BusinessFactAdapter 查询并生成 BusinessFactEvidence。
- 经治理、允许跨案例复用的知识文本：属于知识投影，使用稳定 source entity key 接入 RAGFlow 并生成 RagCitationEvidence。
- 两者可以关联到同一个 source entity key，但不得共用 document ID、ACL 判断或“最新值”语义。
- 未完成脱敏、质量复核或知识发布审批的原始记录，不得因为存在文本字段就自动进入向量库。

## 2. RAGFlow PostgreSQL Data Source 源码审计

### 2.1 审计对象

- version-manifest.json:3-8 锁定 RAGFlow v0.26.4 及上游 commit。
- ragflow/common/data_source/config.py:43-74、ragflow/common/constants.py:141-173 注册 PostgreSQL 数据源类型。
- ragflow/common/data_source/rdbms_connector.py:26-34 和 ragflow/rag/svr/sync_data_source.py:2042-2046,2170-2172 是本次审计的原生 RDBMS 同步实现。
- ragflow/agent/tools/exesql.py:40-67,95-136 是运行时自由 SQL 工具，不创建 Dataset / Document / Chunk，Phase 3 禁止使用。

### 2.2 能力与证据

| 能力 | 源码证据 | 审计结论 |
|---|---|---|
| PostgreSQL 配置入口 | ragflow/web/src/pages/user-setting/data-source/constant/index.tsx:18-44,330-339,1434-1507 | 支持 host、port、database、username/password、query、content_columns、metadata_columns、id_column、timestamp_column |
| Connector API | ragflow/api/apps/restful_apis/connector_api.py:49-113,152-178 | 支持创建、更新、重调度、rebuild、删除 connector；配置原样持久化 |
| 自定义 SQL | ragflow/common/data_source/rdbms_connector.py:99-120,230-239,440-451 | sanitize 只去 Markdown 围栏，未限制 SELECT；必须用固定审核查询和数据库只读账号 |
| 默认取表 | ragflow/common/data_source/rdbms_connector.py:213-239 | query 为空会枚举 public schema 并 SELECT *；生产禁止使用自动全表模式 |
| Content 投影 | ragflow/common/data_source/rdbms_connector.py:122-131,368-378,392-438 | 每行生成一个文本 Document；必须显式列出 content_columns，防止 ACL / 状态字段进入 embedding |
| Metadata 投影 | ragflow/common/data_source/rdbms_connector.py:402-413,429-438 | 仅复制配置列；list / dict 被序列化为 JSON 字符串，不具备可靠的多值 ACL 语义 |
| 文档稳定 ID | ragflow/common/data_source/rdbms_connector.py:380-390；ragflow/rag/svr/sync_data_source.py:233-239 | 配置稳定、唯一、非空 id_column 时可幂等；不识别真实 PK，不支持原生复合 PK，ID 也不含 schema / table |
| 增量 cursor | ragflow/common/data_source/rdbms_connector.py:317-347,510-612；ragflow/rag/svr/sync_data_source.py:282-290,2014-2028 | 支持 timestamp cursor；没有 timestamp 时每轮全量；迟到更新、空 timestamp、未推进 timestamp 均有漏数风险 |
| 正文更新与重新 embedding | ragflow/api/db/services/file_service.py:546-560；ragflow/api/db/services/document_service.py:974-997；ragflow/api/db/services/task_service.py:507-598；ragflow/rag/svr/task_executor.py:1289-1341,1584-1633 | 正文 hash 变化会重新排解析任务、删除旧 chunk 并重新 embedding / 写索引 |
| Dataset → Document → Chunk | ragflow/api/db/services/connector_service.py:488-523；ragflow/rag/svr/sync_data_source.py:214-264；ragflow/api/db/services/file_service.py:514-611；ragflow/api/db/services/document_service.py:1221-1240 | 原生链路完整，可复用；connector sync done 只代表入队，不代表 parsing / embedding / ES 已完成 |
| Metadata 检索前过滤 | ragflow/api/db/services/doc_metadata_service.py:16-21,347-430,815-857；ragflow/api/apps/restful_apis/chunk_api.py:355-376 | 原生 metadata index 可先解析 doc IDs 再检索，但调用方必须显式启用 |
| 删除同步 | ragflow/api/db/services/connector_service.py:151-204,257-328；ragflow/api/db/services/file_service.py:675-718 | sync_deleted_files 默认关闭；开启后按 prune 周期最终一致删除，unlink / delete connector 本身不清理已有文档 |
| 手动与调度 | ragflow/api/db/services/connector_service.py:94-148,257-395,488-506 | 支持周期同步、新绑定立即全量、resume / rebuild；没有单行或单表 sync-now |
| 原生校验 | ragflow/common/data_source/rdbms_connector.py:640-672 | 只执行 SELECT 1，不验证 SQL 只读性、列、ID 唯一性、timestamp 类型或知识投影契约 |
| 真实测试覆盖 | ragflow/test/unit_test/rag/test_sync_data_source.py:264-326 | 使用 Fake RDBMS connector，未覆盖真实 PostgreSQL query、metadata、PK、cursor、delete 和 ACL 负向场景 |

### 2.3 两个硬缺陷

**PG-01：metadata-only 更新不会落索引。**

1. 正文 blob 和 metadata 分开构造：ragflow/common/data_source/rdbms_connector.py:368-413。
2. 已存在文档只有正文 content hash 改变时才进入 doc_blob_pairs：ragflow/api/db/services/file_service.py:531-560。
3. metadata 更新只遍历 doc_blob_pairs：ragflow/api/db/services/connector_service.py:456-472。
4. 同步无立即错误时 cursor 仍会推进：ragflow/rag/svr/sync_data_source.py:282-290。

因此，department、group、business_status 或其他 ACL metadata 变化而正文不变时，RAGFlow 会保留旧 metadata，且后续增量可能不再读到该变化。

**PG-02：metadata 按 filename / semantic_identifier 而非稳定 doc ID 关联。**

- semantic_identifier 取第一 content 列并截断：ragflow/common/data_source/rdbms_connector.py:424-426。
- connector_service.py:445-472 以 filename 建 map，再用最终文档名回写 metadata。
- file_service.py:565-568 会对新文档自动重名；既有文档正文更新又不会同步更新旧文档名。

重复标题、截断碰撞、自动重名或首 content 列变化都可能造成 metadata 错配或漏配。该行为对 ACL 不可接受。

### 2.4 冻结结论

| 决策 | 结论 |
|---|---|
| 是否复用原生 PG Data Source | **是，但仅用于知识正文接入、解析、Chunk、Embedding 和索引。** |
| 是否自建 embedding pipeline | **否。** |
| 是否使用自动 public 全表模式 | **否。** 必须使用管理员审核的固定 SELECT / 只读视图 |
| 是否把原生 metadata 当作 ACL 权威源 | **否。** 只能作为非安全检索属性或可重建缓存 |
| 是否直接暴露 connector SQL / credentials 给 QueryPlan、LLM 或浏览器 | **否。** |
| 是否现在修改上游 | **否。** 先通过企业映射、对账和公开 API 解除；失败后走 CHANGE-REQUEST + ADR |

知识源进入生产前必须同时满足：固定 SELECT、专用只读账号、显式 content_columns、全局唯一非空稳定合成 id_column、可靠 timestamp_column、auto_parse=1、已审计的删除策略，以及对 parsing / embedding / index 完成态的独立检查。

## 3. 数据分类矩阵

“SQL”表示由 BusinessFactAdapter 返回精确事实；“RAG”表示由 RAGFlow 检索知识投影；“Hybrid”表示同一问题需要两路证据，而不是把 SQL 数据先向量化后只走 RAG。

| 数据项 | 权威形态 | 默认路由 | RAG 投影 | SQL 查询 | 证据要求 |
|---|---|---|---|---|---|
| 设备台账 | 结构化业务事实 | SQL | 可有设备介绍，但不得替代台账 | get_equipment_summary | BusinessFactEvidence |
| 设备当前状态 | 当前业务状态字段 | SQL | 禁止用旧文档回答“当前” | get_current_equipment_state | BusinessFactEvidence，带 asOf |
| 实时采集 / 高频遥测 | 遥测或时序服务 | 默认不纳入 P0 | 禁止批量 embedding | Phase 3 不提供通用查询 | unsupported；需独立 SLA 后再冻结 |
| 维修记录结构化字段 | 结构化业务事实 | SQL | 可投影索引摘要，但非权威 | list_recent_repairs / get_repair_record | BusinessFactEvidence |
| 故障描述 | 原始记录 + 可复用知识文本 | Hybrid | 仅治理后的知识投影 | 原始记录通过 get_repair_record / get_fault_history | 两类 evidence 分开 |
| 原因分析 | 原始分析事实 + 经验知识 | Hybrid | 经复核的原因经验 | 原始记录通过 get_repair_record | 两类 evidence 分开 |
| 处理方案 | 实际执行动作 + 可复用方法 | Hybrid | 经复核的处理方法 / SOP | 实际动作通过 get_repair_record | 两类 evidence 分开 |
| 维修时间 | 精确业务事实 | SQL | 禁止从知识文本推断 | list_recent_repairs / get_repair_record | BusinessFactEvidence |
| 维修次数 | 定义明确的聚合事实 | SQL | 禁止按召回文档数计算 | get_equipment_service_statistics | BusinessFactEvidence，带窗口 |
| 保养记录 | 实际业务事实 | SQL | 可有保养说明，但非实际执行记录 | list_recent_maintenance | BusinessFactEvidence |
| 保养周期 / 是否到期 | 实际记录 + 手册 / SOP 周期 | Hybrid | 周期规则、手册或 SOP | 最近保养事实 | 两类 evidence 分开，答案显示比较基准 |
| 设备说明书 | 文档知识 | RAG | 现有文件同步或知识 PG 投影 | 无 | RagCitationEvidence |
| SOP | 文档知识 | RAG | 现有文件同步或知识 PG 投影 | 无 | RagCitationEvidence |
| 故障码含义 / 处置 | 当前发生事实 + 手册解释 | Hybrid | 故障码解释和处置知识 | 当前 / 历史发生记录 | 两类 evidence 分开 |

禁止把整个“维修记录”统一归入 RAG 或 SQL。字段用途、是否要求当前值、是否需聚合以及是否经过知识治理共同决定路由。

## 4. KnowledgeSourceAdapter 契约

### 4.1 责任

冻结实现目录由 3A 独占：enterprise/adapters/knowledge_source/。

KnowledgeSourceAdapter 只负责把已批准的知识投影绑定到 RAGFlow 原生 Data Source，并观察同步、解析和映射结果。它不解析正文、不切 chunk、不调用 embedding 模型，也不自行写 ES。

冻结协议：

~~~text
ensure_binding(command: EnsureKnowledgeSourceBinding) -> KnowledgeSourceBinding
trigger_sync(command: TriggerKnowledgeSync) -> KnowledgeSyncRun
get_sync_status(query: GetKnowledgeSyncStatus) -> KnowledgeSyncStatus
reconcile_documents(command: ReconcileKnowledgeDocuments) -> ReconciliationReport
disable_binding(command: DisableKnowledgeSourceBinding) -> KnowledgeSourceBinding
~~~

### 4.2 请求与返回模型

**EnsureKnowledgeSourceBinding**

| 字段 | 类型 | 规则 |
|---|---|---|
| requestId | string | 必填，幂等追踪 |
| tenantId | string | 必填，只能来自服务端 principal |
| logicalSource | string | 必填，稳定逻辑源名，不是物理表名 |
| profileRef | string | 必填，引用管理员批准的静态连接 / 查询 profile |
| profileVersion | string | 必填，防止静默漂移 |
| ragflowDatasetId | string | 必填，目标 Dataset 已存在且归属已验证 |
| contentProjectionVersion | string | 必填 |
| metadataProjectionVersion | string | 必填 |
| mappingContractVersion | string | 必填 |

命令中禁止出现 password、token、connectionString、host、port、database、query、table、column、SQL 片段或 embedding 参数。profileRef 在服务端解析为 secret 引用和固定查询；任何 profile 变更都必须经配置审查。

**KnowledgeSourceBinding**

| 字段 | 类型 | 规则 |
|---|---|---|
| bindingId | opaque string | Enterprise 稳定 ID |
| tenantId / logicalSource | string | 与命令一致 |
| ragflowConnectorId | string | 仅服务端可见 |
| ragflowDatasetId | string | 已验证归属 |
| profileVersion | string | 实际应用版本 |
| state | disabled / ready / syncing / degraded / failed | 数据源生命周期，不等同消息业务状态 |
| lastSuccessfulSyncAt | datetime or null | 只表示 adapter 同步成功 |
| lastIndexedAt | datetime or null | 仅在 parsing / embedding / index 检查完成后更新 |

**KnowledgeDocumentRef**

| 字段 | 类型 | 规则 |
|---|---|---|
| tenantId | string | 必填 |
| logicalSource | string | 必填 |
| sourceEntityKey | opaque string | 从稳定合成 PK 生成；禁止标题作为 key |
| ragflowDatasetId | string | 必填 |
| ragflowDocumentId | string | 必填 |
| sourceUpdatedAt | datetime | 必填 |
| contentHash | string | 可选，禁止暴露正文 |
| aclFactRef | opaque string | 指向企业 ACL 权威事实或映射 |
| aclPolicyVersion | string | 必填 |
| mappingState | ready / stale / denied / missing | 非 ready 不得进入正式检索 scope |

### 4.3 不变量

- id_column 必须由固定 SQL 生成全局唯一、非空、稳定的 sourceEntityKey；禁止使用标题或正文 hash 作为生产 ID。
- timestamp_column 必须非空且能覆盖正文、知识发布状态和 ACL 权威事实的所有相关变化；否则必须走全量对账。
- 原生 metadata-only 更新不足以证明 ACL 已刷新。reconcile_documents 必须按 sourceEntityKey / RAGFlow document ID 校验企业映射，禁止按 filename 对账。
- FormalScopeResolver 只允许 mappingState=ready 的文档进入 doc_ids pre-filter。当前 enterprise/gateway/query/formal_router.py:240-309 只读取 ext_document_map，因此 3A 必须产出可被 Lead 串行接入该映射的结果。
- connector sync done 后还必须验证 Document parsing 状态及索引结果；不得把“已入队”报告为“可检索”。
- 源删除默认 fail-closed。只有 stable ID、完整 snapshot 和客户确认删除语义均通过后，才允许启用 prune。
- 所有管理操作写安全审计；日志只记录 ID、状态、数量、耗时和错误码，不记录查询文本、凭据、正文或完整 metadata。

## 5. BusinessFactAdapter 契约

### 5.1 责任和目录

冻结实现目录由 3B 独占：enterprise/adapters/business_db/。

BusinessFactAdapter 是客户业务 PostgreSQL 的唯一问答读取入口。它以专用只读账号执行预注册、参数化、带权限约束和上限的命名查询。不得提供 execute_sql、query_table、传入 where / orderBy / column 的接口。

### 5.2 冻结方法

~~~text
resolve_equipment_ref(context, EquipmentRef) -> ResolvedEquipmentRef
get_equipment_summary(context, ResolvedEquipmentRef) -> BusinessFactResult<EquipmentSummary>
get_current_equipment_state(context, ResolvedEquipmentRef) -> BusinessFactResult<EquipmentState>
list_recent_repairs(context, RecentRepairQuery) -> BusinessFactResult<RepairRecord[]>
get_repair_record(context, RepairRecordQuery) -> BusinessFactResult<RepairRecord>
get_equipment_service_statistics(context, ServiceStatisticsQuery) -> BusinessFactResult<ServiceStatistics>
list_recent_maintenance(context, RecentMaintenanceQuery) -> BusinessFactResult<MaintenanceRecord[]>
get_fault_history(context, FaultHistoryQuery) -> BusinessFactResult<FaultOccurrence[]>
~~~

resolve_equipment_ref 是受权限约束的内部解析步骤，不允许 QueryPlan 直接把其结果视为已授权，也不生成面向用户的事实证据。其余七个方法对应 QueryPlan 的闭集 operation。

### 5.3 公共输入

**BusinessQueryContext**

| 字段 | 来源 | 规则 |
|---|---|---|
| requestId | Gateway | 必填 |
| principal | Phase 2 UserPrincipal | 必填，不从模型参数构造 |
| aclScope | BusinessAclScope v1 | 必填，执行前解析 |
| deadline | Gateway | 必填 |
| queryProfileVersion | 服务端配置 | 必填 |

**EquipmentRef** 必须且只能含一个候选标识：

- equipmentId
- equipmentCode
- fixedAssetNo
- assetId

候选值只用于受控解析，不能绕过 canonical entity key 和 ACL 校验。

**查询边界**

- recent repairs、recent maintenance、fault history：limit 默认 10，硬上限 20。
- 时间窗口最大 366 天；统计方法必须返回 windowStart、windowEnd 和 asOf。
- 单记录方法最多返回 1 条；唯一性异常必须失败，不得任取第一条。
- 默认 DB statement timeout 2 秒，硬上限 5 秒；外层 deadline 更短时以更短者为准。
- 不支持 offset 全量翻页；问答链路不得用 continuation token 拉取超过上限的结果。

### 5.4 逻辑返回字段

物理表、列、JOIN 和实际类型映射必须由客户数据字典解除 blocker 后冻结；以下是 Gateway 的稳定逻辑字段，不表示已经确认客户 schema。

| DTO | 最小逻辑字段 |
|---|---|
| EquipmentSummary | entityKey、equipmentId、equipmentCode、fixedAssetNo、name、model、serialNo、departmentId、location、businessStatus、updatedAt |
| EquipmentState | entityKey、stateCode、stateLabel、stateSince、updatedAt、asOf |
| RepairRecord | entityKey、recordId、faultCode、faultDescription、causeAnalysis、handlingSolution、repairStartedAt、repairEndedAt、status、updatedAt |
| ServiceStatistics | entityKey、repairsCount、maintenanceCount、lastRepairAt、lastMaintenanceAt、windowStart、windowEnd、asOf |
| MaintenanceRecord | entityKey、recordId、maintenanceType、description、performedAt、completedAt、status、updatedAt |
| FaultOccurrence | entityKey、recordId、faultCode、description、occurredAt、resolvedAt、status、updatedAt |

不存在或不适用的可选字段返回 null，不得臆造。每个 DTO 只能返回 allowlist 字段，禁止透传 SELECT * 行对象。

**BusinessFactResult**

| 字段 | 类型 | 规则 |
|---|---|---|
| operation | 闭集 enum | 必填 |
| logicalSource | string | 必填，不暴露物理 schema |
| rows | typed DTO or DTO[] | 已授权、已脱敏 |
| asOf | datetime | 必填 |
| truncated | boolean | 必填 |
| evidenceItems | BusinessFactEvidence[] | 与返回事实一一可追溯 |
| execution | object | 仅 requestId、profileVersion、durationMs；禁止 SQL、参数、连接信息 |

### 5.5 SQL 安全不变量

- 每个方法只能绑定一个已审查的 SQL template 或 stored procedure version。
- 所有用户值和实体值必须参数化；表名、列名、JOIN、排序和权限谓词不能来自 QueryPlan 或自然语言。
- tenant、department / group、asset / equipment 权限必须在 SQL 执行前作为谓词或固定 ACL JOIN 注入；禁止先读全量结果再删除未授权行。
- 数据库账号必须由 PostgreSQL 强制只读；DML、DDL、COPY、扩展安装和任意函数执行均应被拒绝。
- 超时、行数和字段 allowlist 在 adapter 内强制执行，不能仅依赖 prompt。
- 错误和审计不得记录 password、DSN、完整 SQL、完整参数、客户正文或原始模型响应。

## 6. QueryPlan 契约

### 6.1 模型

QueryPlan 由 rule-first planner 产生，并在任何数据访问前进行严格校验。

~~~json
{
  "version": "phase3.query-plan/v1",
  "route": "hybrid",
  "intent": "maintenance_due_assessment",
  "entities": {
    "equipment": [
      {"kind": "equipment_code", "value": "EQ-001"}
    ],
    "faultCodes": []
  },
  "ragQuery": "该设备型号的保养周期和检查项目",
  "businessCalls": [
    {
      "callId": "b1",
      "operation": "list_recent_maintenance",
      "arguments": {
        "equipmentRef": {"kind": "equipment_code", "value": "EQ-001"},
        "limit": 10
      }
    }
  ]
}
~~~

| 字段 | 规则 |
|---|---|
| version | 必须精确为 phase3.query-plan/v1 |
| route | 只能为 rag、business、hybrid |
| intent | Gateway 闭集 intent；未知 intent 拒绝 |
| entities.equipment | 最多 3 个候选；每项 kind 只能为 equipment_id、equipment_code、fixed_asset_no、asset_id |
| entities.faultCodes | 最多 5 个规范化候选 |
| ragQuery | rag / hybrid 必填；business 必须为空 |
| businessCalls | business / hybrid 至少 1 项，最多 3 项；rag 必须为空 |
| operation | 只能为第 5.2 节七个面向用户的命名方法 |
| arguments | 按 operation 使用严格 DTO；拒绝未知字段 |

### 6.2 校验和执行规则

- unsupported 和 unsafe 是 planner 的拒绝结果，不是合法 QueryPlan.route；拒绝后不得访问 RAGFlow 或业务库。
- QueryPlan 中严禁出现 sql、query、table、schema、column、select、where、join、orderBy、connection、credential、host 等数据访问控制字段。
- 实体识别结果只是候选。Gateway 必须先规范化为 canonical entity key，再解析 BusinessAclScope；不能把模型抽取出的 ID 直接带入查询。
- businessCalls 的 callId 只用于融合关联，不是数据库 ID。
- route=rag 只编译现有 AclScope / doc_ids 后检索。
- route=business 只执行已授权命名操作，不为了“补充背景”隐式调用 RAG。
- route=hybrid 两路都成功、或按显式降级规则得到可靠结果后才合成；任何分支不得复用另一分支的 ACL 结论。
- topK、SQL timeout、最大行数、模型、prompt template 和连接 profile 均由服务端策略决定，不属于 QueryPlan。
- 会话中的旧实体和本轮新实体冲突时必须触发现有显式 entity switch 语义；禁止静默跨设备。

## 7. 统一 Evidence 契约

### 7.1 判别联合

Gateway 内部使用 phase3.evidence/v1 判别联合。外部 API 在兼容期继续使用 citations 数组和 sourceType=document|business_record；新增字段只能由 Lead 以 additive 方式更新 integration-openapi.yaml。

**RagCitationEvidence**

| 字段 | 规则 |
|---|---|
| evidenceId | 稳定 ID；对外可投影为现有 citationId |
| sourceType | 固定为 document |
| datasetId、documentId、chunkId | RAGFlow 引用键 |
| title、pageNo、quote、score | 沿用现有 Citation 语义 |
| sourceEntityKey | 有知识源映射时提供 |
| createdAt | 生成证据快照时间 |

**BusinessFactEvidence**

| 字段 | 规则 |
|---|---|
| evidenceId | 必填、消息内唯一 |
| sourceType | 固定为 business_record |
| operation | 第 5.2 节闭集 operation |
| logicalSource | 逻辑源名，禁止物理表名 |
| entityKey | canonical、opaque、必填 |
| recordType | equipment / equipment_state / repair / maintenance / fault / service_statistics |
| recordId | 单记录时必填；聚合记录使用稳定 synthetic record ID |
| fact | allowlist 字段的不可变 JSON 快照 |
| asOf | 源记录 / 聚合事实的有效时间 |
| createdAt | 证据快照生成时间 |
| queryProfileVersion | 固定查询 profile 版本 |
| aclPolicyVersion | 生成时的权限策略版本 |

fact 中禁止存储完整 SQL、SQL 参数、DSN、secret、不可展示内部字段、完整 Prompt 或原始模型响应。

### 7.2 生命周期

- Evidence 与 assistant message 在完成态一次性持久化；历史回放读取持久化快照，不重新查询业务库，也不按当前值改写历史。
- 当前 enterprise/gateway/query/conversation_store.py:35-106,319-387 已具备 message / citation snapshot 基础，但 business_record 仍只是占位字段；Phase 3 由 Lead 串行扩展。
- 历史回放如需展示详细 fact，必须先按当前 principal 对 entityKey 重新授权；授权通过后返回原快照，授权失败则 fail-closed / 脱敏，不得用“重新查询当前记录”代替旧快照。
- citation detail 按 sourceType 分派：document 使用现有文档 ACL；business_record 使用业务实体 ACL。当前 formal_router.py:991-1063 只走 document 判断，必须由 Lead 串行集成。
- completed、no_reliable_evidence、failed 由运行结果显式决定；不得根据 evidence 是否为空推导，反向也不成立。
- Hybrid 回答至少保留一条 RagCitationEvidence 和一条 BusinessFactEvidence，除非命中了明确、可审计的单分支降级规则。

## 8. ACL 契约

### 8.1 复用与新增边界

- 复用 Phase 2 UserPrincipal：enterprise/gateway/auth/user_principal.py:12-29。
- 复用 deny-first、fail-closed 和无旁路原则：contracts/acl-design-freeze.md:12-39。
- 现有 AclScope 仅表达 Dataset / Document / metadata predicate：enterprise/gateway/acl/schema.py:44-102；不得把业务行权限偷偷塞入该模型。
- enterprise/gateway/acl/context.py:11-21 已明确业务 PostgreSQL 运行事实留待后续；Phase 3 新增独立 BusinessAclScope。

### 8.2 BusinessAclScope v1

~~~json
{
  "version": "phase3.business-acl-scope/v1",
  "tenantId": "tenant-a",
  "departmentIds": ["dept-1"],
  "groupIds": ["group-1"],
  "scopeMode": "entity_keys",
  "authorizedEntityKeys": ["opaque-entity-key"],
  "authorityScopeRef": null,
  "policyVersion": "policy-v1",
  "resolvedAt": "2026-08-09T00:00:00Z"
}
~~~

规则：

- tenantId 必填且只能来自验证后的 UserPrincipal。
- scopeMode 只能为 entity_keys 或 authority_ref，二者的 authorizedEntityKeys / authorityScopeRef 必须恰好一个有效。
- 空集合、解析失败、策略版本未知、超时或客户 ACL 事实缺失均返回 deny；不存在隐式 wildcard。
- departmentIds 和 groupIds 是补充约束，不自动等价于设备 / 资产权限。
- scope 不返回客户端，不允许由 QueryPlan、LLM 或请求正文提供。
- 大权限集必须用固定 ACL 关系 / 安全视图或 authorityScopeRef 在数据库侧 JOIN，禁止为了方便把全量 ID 拉到应用内再过滤。

### 8.3 执行顺序

1. 从 UserPrincipal 和本轮候选实体开始。
2. 在租户约束内将 EquipmentRef 规范化为 canonical entity key，且不得泄露未授权实体是否存在。
3. 调用企业权限事实解析器得到 BusinessAclScope。
4. 将 tenant、department / group、asset / equipment 的约束编译到固定 SQL 谓词或固定 ACL JOIN。
5. 在数据库内只读取已授权行。
6. 返回后只做 schema allowlist、脱敏和完整性校验；不得把返回后过滤当作 ACL。
7. 生成 BusinessFactEvidence 并记录 policyVersion。

如果客户业务表不携带权限列，只接受两种经客户确认的模式：

- 固定 ACL relation / security view 在查询内 JOIN；或
- 权限服务返回可验证的 authorityScopeRef / 受限 entity keys，再作为强制谓词。

选择哪种模式、资产与设备的关系以及 deny / inactive / deleted 语义当前仍是 blocker。

### 8.4 RAG 知识 ACL

- 继续由 FormalScopeResolver 预先编译 RAGFlow doc_ids；禁止先全库召回再删结果。
- 原生 PostgreSQL metadata 只能作为可重建缓存，不能替代 ext_document_map / 企业权限事实。
- 3A 必须提供 sourceEntityKey → ragflowDocumentId → aclFactRef 的稳定对账结果；mappingState 非 ready 时文档不得进入检索范围。
- PG-01 或 PG-02 未解除前，即使 RAGFlow metadata 看起来正确，也不能据此判定访问允许。

## 9. Phase 3A / 3B / 3C 并行开发边界

并行开发必须在本契约冻结后开始。三个子任务只能新增或修改各自独占目录及独占测试目录；共享集成由 Lead 串行完成。

| 子任务 | 独占写目录 | 交付物 | 禁止修改 |
|---|---|---|---|
| 3A Knowledge Source | enterprise/adapters/knowledge_source/**；enterprise/tests/wp04_phase3_3a/** | RAGFlow PG binding / sync / status / document reconciliation adapter；fake 与真实契约测试 | ragflow/**、enterprise/gateway/sync/**、enterprise/gateway/query/**、contracts/**、共享 conftest、迁移、锁文件 |
| 3B Business Facts | enterprise/adapters/business_db/**；enterprise/tests/wp04_phase3_3b/** | 七个命名只读操作、实体解析、SQL template registry、字段 allowlist、timeout / limit、契约测试 | auth/**、acl/**、query/**、config.py、app.py、contracts/**、ragflow/**、迁移、锁文件 |
| 3C Planning / Fusion / Evidence | enterprise/gateway/query/phase3/**；enterprise/tests/wp04_phase3_3c/** | rule-first QueryPlan、严格 validator、orchestrator / fusion、evidence domain model，以 port / fake 调用 3A / 3B | formal_router.py、conversation_store.py、ragflow_client.py、app.py、config.py、contracts/**、auth/**、acl/**、ragflow/** |
| Lead 串行集成 | contracts/**、enterprise/gateway/app.py、config.py、query/formal_router.py、query/conversation_store.py、query/ragflow_client.py，以及必要共享模型 | OpenAPI / error code additive 更新、DI、正式路由、持久化、citation detail 分派、SSE 与回归 | 不做无关重构，不扩大到 WP-05 / Phase 4 |

共同规则：

- 三个子任务不得共享写同一个测试文件或 conftest。
- 发现契约不足时只提交 CHANGE-REQUEST.md，禁止自行改主 OpenAPI、error code、metadata schema 或上游源码。
- 不修改 enterprise/web、deploy/overlays、WP-01 / WP-02 模块。
- 新依赖、数据库迁移、根锁文件由 Lead 单独评审；默认优先复用现有库。
- 每个子任务必须报告修改文件、行为、测试、配置 / 契约变化、风险、是否改上游和集成注意事项。

## 10. 集成点与执行顺序

1. **Lead 注册依赖。** 在 app.py / config.py 中注入 KnowledgeSourceAdapter、BusinessFactAdapter、BusinessScopeResolver 和 Phase 3 planner；配置只引用 secret / query profile，不接受请求期 SQL。
2. **沿用 Phase 2 入口保护。** Formal ask / stream / history / citation detail 继续先做 authentication、capability、tenant 和 conversation owner 校验。
3. **解析会话实体。** 合并当前请求和已持久化上下文；冲突时走显式 entity switch，不能静默切换设备。
4. **生成并校验 QueryPlan。** unsupported / unsafe 在此终止；合法 plan 严格拒绝未知字段和自由 SQL 能力。
5. **分别解析权限。** RAG 分支编译现有 AclScope / doc_ids；SQL 分支解析 BusinessAclScope。两者互不替代。
6. **执行 RAG 分支。** 只在企业映射为 ready 的文档范围内调用现有 RAGFlow 客户端，生成 RagCitationEvidence。
7. **执行 SQL 分支。** 只调用 plan 中的命名 operation；adapter 在 SQL 内强制 tenant / department / group / entity 约束，生成 BusinessFactEvidence。
8. **融合。** orchestrator 按闭集 intent 组合结构化事实与知识解释；不得让 LLM 看到凭据、SQL、物理 schema 或未授权候选。
9. **显式决定 outcome。** completed / no_reliable_evidence / failed 由分支结果与质量规则决定，和 evidence 数量解耦。
10. **原子持久化快照。** assistant message、业务状态和两类 evidence 一次写入；失败不得留下伪 completed 消息。
11. **保持 SSE 兼容。** 继续使用 Phase 2 事件与顺序；citation 事件通过 sourceType 区分 document / business_record，不新增隐式终止语义。
12. **按来源分派详情。** document citation 走当前文档 ACL；business_record evidence 走当前业务实体 ACL，授权后返回历史快照。
13. **安全审计。** 记录 requestId、route、operation、logicalSource、scope policy version、行数 / evidence 数和耗时；不记录正文、完整 Prompt、原始模型响应、SQL 或 secret。

Lead 集成前，3A / 3B / 3C 只能通过本契约定义的 ports 和 fakes 相互验证，不得抢先修改 formal_router.py 或 conversation_store.py。

稳定错误映射沿用 contracts/error-codes.yaml:63-71：

| 场景 | 稳定错误码 | 规则 |
|---|---|---|
| intent / operation 不在闭集，或精确查询不支持 | BUSINESS_QUERY_NOT_SUPPORTED | 在数据访问前拒绝 |
| BusinessAclScope 为空、无法解析或实体未授权 | BUSINESS_QUERY_DENIED | fail-closed，不泄露实体是否存在 |
| SQL statement timeout 或 adapter deadline 超时 | BUSINESS_QUERY_TIMEOUT | 不自动扩大 timeout，不返回部分未验证事实 |

连接不可用、schema / profile version 不匹配等错误目前没有已冻结的业务错误码，不得擅自复用 TIMEOUT 或 DENIED；由 Lead 在 B-10 中决定是否 additive 新增 BUSINESS_QUERY_UNAVAILABLE。

## 11. 验收标准

### 11.1 契约与安全

| ID | 验收条件 |
|---|---|
| AC-01 | QueryPlan 对 route 组合、operation、arguments 和 entity kind 做严格校验；未知字段拒绝 |
| AC-02 | 包含 sql / table / schema / column / where / join / credentials 等自由访问字段的 plan 全部拒绝，且未触发任何数据访问 |
| AC-03 | BusinessFactAdapter 仅暴露冻结命名方法；无 execute_sql 或任意查询 escape hatch |
| AC-04 | 使用数据库只读账号验证 SELECT 成功、INSERT / UPDATE / DELETE / DDL / COPY 失败 |
| AC-05 | 注入样例只能作为绑定参数；SQL 结构、排序、表和列不发生改变 |
| AC-06 | tenant、department / group、asset / equipment 权限在数据库查询前强制；未授权实体返回零行或稳定 denied，不能通过计数、错误或耗时枚举存在性 |
| AC-07 | limit、366 天窗口、2 / 5 秒 timeout 和字段 allowlist 均由 adapter 强制 |
| AC-08 | 日志、审计、API、SSE 和 evidence 不含 DSN、secret、完整 SQL、完整参数、完整 Prompt 或原始模型响应 |

### 11.2 RAGFlow PostgreSQL 真实链路

| ID | 验收条件 |
|---|---|
| AC-09 | 真实 PostgreSQL 固定视图用稳定合成 PK 首次同步后产生预期 Dataset / Document / Chunk / ES 记录 |
| AC-10 | 相同数据重复同步不重复创建文档；正文变化保持同一 sourceEntityKey 并触发重新解析和 embedding |
| AC-11 | ACL / 发布状态仅 metadata 变化时，企业 mapping / scope 可被可靠更新或立即 fail-closed；不得复现 PG-01 的旧权限放行 |
| AC-12 | 重复标题、标题变化和截断碰撞不影响 sourceEntityKey → documentId → ACL 映射；不得按 filename 授权 |
| AC-13 | 源删除在已确认 prune 策略后清理 Document、metadata 和 chunk；unlink / connector delete 的残留语义有显式测试 |
| AC-14 | connector sync done 与 indexed ready 分开断言；只有 parsing / embedding / ES 完成且 mappingState=ready 才允许正式召回 |
| AC-15 | 未授权用户在 RAGFlow 请求发出前得到空 doc_ids / deny；禁止先全库召回再过滤 |

### 11.3 路由、融合和证据

| ID | 验收条件 |
|---|---|
| AC-16 | 覆盖 rag-only、business-only、hybrid、unsupported、unsafe 五类用户问题 |
| AC-17 | 设备状态、维修时间、次数只来自 SQL；说明书 / SOP 只来自 RAG；保养周期与故障码场景分别保留两路 evidence |
| AC-18 | Hybrid 分支错误、超时、空结果和降级规则均有确定 outcome；状态不按 evidence 数量推导 |
| AC-19 | BusinessFactEvidence 包含 operation、logicalSource、entityKey、allowlist fact、asOf、createdAt；RAG evidence 保持现有 citation 兼容 |
| AC-20 | 历史回放不重查业务库、不漂移旧 fact；当前授权通过时返回原快照，权限撤销后详细 evidence fail-closed |
| AC-21 | conversation / message / citation IDOR、capability denial、entity switch 和 SSE event ordering 回归通过 |
| AC-22 | 所有权限负向测试使用非敏感 fixture，Phase 3 强制环境验收不得 skip、xfail、删断言或伪造报告 |

### 11.4 验证命令

当前 Contract Freeze 的文档验证：

~~~powershell
git diff --check
git status --short
~~~

Phase 2 回归基线：

~~~powershell
python -m pytest enterprise/tests/test_formal_query.py enterprise/tests/test_acl_core.py enterprise/tests/test_wp01a.py enterprise/tests/test_query_contract.py -q
python enterprise/scripts/run_wp04_phase2_e2e.py
~~~

Phase 3 实施后必须新增并在真实 PostgreSQL + RAGFlow v0.26.4 环境运行：

~~~powershell
python -m pytest enterprise/tests/wp04_phase3_3a enterprise/tests/wp04_phase3_3b enterprise/tests/wp04_phase3_3c -q
~~~

不得仅用 Fake connector 替代 AC-09 至 AC-15。真实 E2E 必须检查索引完成态，而非只检查 connector task done。

## 12. Blockers 与开工门槛

| ID | 级别 | 阻塞项 | 影响 | 解除条件 / 责任 |
|---|---|---|---|---|
| B-01 | P0 | 客户业务 PostgreSQL 表、列、类型、关系和样例数据字典缺失 | 无法冻结物理 SQL、DTO 映射和真实契约测试 | 客户 / Lead 提供非敏感 schema、视图或脱敏 fixture |
| B-02 | P0 | 各知识投影的稳定 PK、复合 key、updated timestamp、删除和迟到更新语义未确认 | 无法保证幂等、cursor 和 prune 安全 | 数据 owner 逐逻辑源签字确认 |
| B-03 | P0 | tenant / department / group / asset / equipment 的行级 ACL 事实与 allow / deny / inactive 语义未映射 | 无法生成 BusinessAclScope 或 SQL pre-filter | Identity / ACL owner 与客户冻结 relation / view 或 permission-service 契约 |
| B-04 | P0 | RAGFlow PG-01 metadata-only 丢更新、PG-02 filename 错配 | 原生 metadata 不能做权威 ACL | 3A 企业映射 / 对账方案通过 AC-11 / AC-12；否则提交上游 CHANGE-REQUEST + ADR |
| B-05 | P0 | 原生 PG 文档尚无 sourceEntityKey → ext_document_map / FormalScopeResolver bridge | Formal API 无法安全召回 PG 知识文档 | 3A 输出稳定 mapping，Lead 串行接入 formal scope |
| B-06 | P0 | 专用 PostgreSQL 只读角色、secretRef、固定视图 / SELECT profile 尚未提供 | 任意 SQL 与凭据风险，真实测试不能执行 | Platform / DBA 提供最小权限账号并验证 DML / DDL 拒绝 |
| B-07 | P1 | “设备当前状态”与高频实时遥测的字段、时效 SLA、降级语义未定义 | 可能把陈旧状态误称为实时 | 产品 / 数据 owner 冻结 current-state 字段与 asOf / staleness 规则；遥测默认出 P0 |
| B-08 | P1 | 维修次数、保养次数、窗口、取消 / 作废记录等统计口径未定义 | 聚合事实不可审计 | 业务 owner 为每个 metric 冻结定义和 fixture |
| B-09 | P1 | JWT claim 名、设备 / 资产授权来源及大权限集表达仍未完全落地 | BusinessScopeResolver 无可靠输入 | Identity / ACL owner 补充正式 claim / authority 契约 |
| B-10 | P1 | integration-openapi 的 Business Evidence 字段、运行时 business error registry、capability 文档仍是 Phase 2 / planned 状态 | 客户端与运行时集成可能漂移 | Lead 只做 additive 契约更新并运行 Phase 2 回归 |

实施门槛：

- B-01 至 B-06 未解除前，不得宣称 Phase 3 实施 PASS，也不得将真实客户业务库接入生产问答。
- 3C 可基于本冻结契约和 fakes 开展纯模型 / validator 测试；3A / 3B 可做不含客户假设的 adapter scaffold，但不得自行猜测 schema、ACL 或统计口径。
- 任何需要修改 ragflow/** 的方案都必须证明配置、公开 API 和 enterprise 外围适配均不可行，并提交 CHANGE-REQUEST.md、ADR、独立测试、兼容风险和回滚方式，由 Lead 决策。
- 本冻结不授权 WP-05、Phase 4、前端扩展、生产部署或任意上游核心修改。
