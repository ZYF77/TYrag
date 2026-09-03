# EAM 业务文档唯一键与 Document Feed 3.2 投喂实施说明

> 受众：EAM/TPM 开发 Agent
> 状态：待 EAM 仓库实施
> 日期：2026-09-01
> 服务端契约：Document Feed 3.2.0、terminal callback 1.0.0
> EAM 工作区：`C:\CodingProgram\WAES\tpm`

## 目标

EAM 负责业务记录身份、来源版本和知识投影内容，通过统一 Document Feed 3.2 投喂 Gateway。EAM 不实现 Gateway 的候选晋升、RAGFlow 启停、质量门或检索 Scope。

继续调用：

```http
POST /enterprise/api/v3/documents
```

不增加 `/facility`、`/repair`、`/maintenance` 等业务接口。

## EAM 职责

- 决定一条业务记录的稳定唯一键并填写 `externalDocumentId`。
- 决定来源系统版本并填写 `sourceVersionId`。
- 对相同业务版本的网络重试保持请求内容、`sourceVersionId` 和 `eventId` 不变。
- 明确 upsert、disable、delete 等业务意图。
- 只投喂允许进入知识库的知识投影；权威业务事实继续保留在 EAM/SQL。
- 处理 202 接受、状态查询和 terminal callback；202 不表示最终可检索。

Gateway 负责解析、质量、当前可检索投影、旧投影停用和 RAGFlow 状态，EAM 不读取或修改 Gateway/RAGFlow 内部表。

## 稳定业务键

EAM 用集中常量/helper 生成 `externalDocumentId`：

```text
设备台账：EQUIPMENT_LEDGER:{DeviceCode}
维修知识：REPAIR:{BillNo}
保养知识：MAINTAIN:{BillNo}
其他单据知识：{BusinessType}:{BillNo}
```

- 设备台账以设备号 `DeviceCode` 为唯一标识。
- 单据知识以实际单据号 `BillNo` 为唯一标识。
- 当前代码中表示单据种类的 `repair_bill`、`maint_bill` 等 BillCode 不等于业务记录单号。
- 前缀只用于避免不同业务类型键碰撞。
- 缺少 DeviceCode/BillNo 时拒绝投喂，不回退数据库内部 Id。
- 同一业务记录后续更新必须继续使用完全相同的 `externalDocumentId`。

当前台账主 JSON 的 `FAC-{row.Id}-MASTER` 应替换为设备号键。

## 来源版本与幂等

- 同一业务记录内容或影响知识权限的 Metadata 变化时生成新 `sourceVersionId`。
- 如果 EAM 有稳定原生 revision/row version，可直接使用；没有时使用稳定 JSON 内容与 ACL Metadata 的 SHA-256 指纹。
- 相同内容失败重投保持相同 `sourceVersionId`。
- `eventId` 由稳定 `externalDocumentId + sourceVersionId + operation` 生成；同一处理轮网络重试不得递增 submit 序号。
- 同一 `sourceVersionId` 内容不同会收到 409 `DOCUMENT_VERSION_CONFLICT`，EAM 必须修正版本身份后重投，不能覆盖旧内容。
- EAM 不通过版本权重或直接操作 RAGFlow 处理旧版本。

## 投喂内容

### 设备台账

- 保留现有几乎完整台账 INLINE_JSON 内容。
- `metadata.equipment_id` 使用 DeviceCode，并与 JSON 中出现的明确设备号保持一致。
- `metadata.document_type` 使用 `EQUIPMENT_LEDGER`。
- 现有 PDF 附件继续按 FILE_SHARE 独立登记，并在主 JSON 中保留附件关系。

### 维修知识

- 将现有 `FeedRepairKnowledgeAsync` 中允许复用的故障描述、原因经验、处理方法、预防措施和知识附件关系组装为一个 INLINE_JSON 主文档。
- `externalDocumentId=REPAIR:{BillNo}`。
- 不把维修时间、次数、当前状态、精确费用、实际执行动作等权威事实改成只由 RAG 返回。

### 保养知识

- 将现有 `FeedMaintainKnowledgeAsync` 中的保养说明、可复用项目经验、规则和知识附件关系组装为一个 INLINE_JSON 主文档。
- `externalDocumentId=MAINTAIN:{BillNo}`。
- 不把实际保养记录、完成时间、次数和到期判断改成只由 RAG 返回。

普通 JSON 字段进入文本/向量索引，不自动成为 Metadata。不要向请求添加 Parser、Chunk、Embedding、Profile、JSONPath 配置或业务内容 `sha256`。

## 状态与 callback

- POST 返回 202 只表示 Gateway 已接受处理。
- 正式成功以 terminal callback `retrievable` 为准。
- `review_required` 表示内容已处理但不能成为当前可检索投影，EAM 应展示原因并允许修正后以新版本重投。
- `failed` 按 `retryable` 决定原请求重试或人工修正。
- callback `deliveryId` 必须幂等处理。
- 新版本处理期间，EAM 不主动停旧 RAGFlow 文档；Gateway 会继续提供旧投影，待新版本可用后切换。

## EAM 实施范围

- 修改台账主 JSON 的 `externalDocumentId` 生成规则。
- 将维修/保养现有旧 orchestrator 知识投影迁移到 `EnterpriseDocumentClient` 的 Document Feed 3.2 请求。
- 抽取稳定键、版本指纹和 eventId helper；不建设通用规则引擎或在线配置平台。
- 复用现有 HMAC、网络重试、日志、状态查询和 callback 汇总。
- 保留无关工作区改动，不修改 TYrag/RAGFlow 源码。

## EAM 测试与交付

- 同一设备多个台账内容版本使用同一 externalDocumentId、不同 sourceVersionId。
- 不同设备互不覆盖。
- 同类型相同 BillNo 形成一条版本链；不同 BillNo、不同业务类型互不覆盖。
- 缺少 DeviceCode/BillNo 时拒绝投喂。
- 相同内容重试幂等；同版本不同内容识别 409。
- 维修/保养 JSON 序列化不包含 secret、文件正文、base64 或业务 `sha256`。
- callback deliveryId 去重、retrievable/review_required/failed 状态映射正确。
- 完成 EAM 单元/序列化测试和 build；与 TYrag 联调时再执行真实 202、RAGFlow Chunk、状态、callback、冲突和安全负例 E2E。

## 明确不做

- 不修改 TYrag、Gateway 或 RAGFlow 源码。
- 不新增 Document Feed 业务接口或修改 3.2 公共 schema。
- 不让 EAM 决定 Gateway `current_version`、RAGFlow enabled/disabled 或质量门。
- 不把完整维修/保养业务事实迁入 RAG。
- 不部署到 `192.168.30.30`；部署需另行明确授权。

## 交付报告

EAM Agent 完成后必须报告：修改文件、唯一键映射、版本/eventId 规则、测试命令与结果、未解决风险，以及需要 TYrag 联调验证的 callback/E2E 项。
